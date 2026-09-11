# Implemented Plan Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the one priority-one runtime defect and three priority-two safety/operability gaps found in post-implementation review of the protected integration and release-test paths, add regression tests at the exact boundary where each defect was observed, and leave the existing safety contracts (fail-closed gates, mutex retention, production refusal, signing eligibility) unchanged.

**Architecture:** Each remediation is an isolated, surgical fix inside the module where the defect lives (`qualification.py`, `migrate.py`, `online_workflows.py`, `scripts/sql/migration_metadata.sql`); no new modules, no architectural change. Recovery context is carried as an optional structured attribute on the existing `MigrationRunError` exception (mirroring the `.report` pattern `QualificationError` and `OnlineWorkflowError` already use), so the fix composes with existing exception handling instead of replacing it.

**Tech Stack:** Python 3.10+ standard library, `unittest`, `unittest.mock.patch`, Oracle SQL/PLSQL (SQLcl-executed, mocked in tests via the existing `sql_runner`/`runner` injection seams).

**Spec:** `docs/superpowers/specs/2026-09-10-implemented-plan-review-remediation-design.md`

## Global Constraints

- Production writes remain refused; protected integration/release-test execution remains limited to prepared non-production targets.
- Migration drift, target identity, mutex, attempt, verification, observation-chain, destructive-confirmation, and recovery gates remain fail-closed.
- Unknown database outcomes retain their mutex until evidence-driven recovery.
- Only protected test PASS evidence may be signed or used for runbook generation.
- No command commits or pushes automatically.
- No secret, credential, wallet content, or database connection string may appear in any new structured output (recovery context, CLI rendering).
- Test-first development: each step below starts with a failing test and ends with that test (and its neighboring suite) passing.
- Preserve every existing test's assertions and error message text; only add new constructor arguments / new tests, never change wording that existing `assertRaisesRegex` patterns depend on.
- Run the exact offline verification commands CI uses before calling this plan done: `python3 -m ruff check scripts/` and `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v`, both from the repository root.
- Section 4 of the design (protected acceptance on real integration/test runners) cannot be executed by an agent with no access to prepared Oracle/APEX infrastructure. Task 8 documents the exact operator steps but cannot be checked complete by this plan's executor.

## File Structure

### Existing files changed

- `scripts/teamlib/qualification.py` — stop wrapping `apply_report` in `Path(...)` before validation (P1 fix).
- `scripts/tests/test_qualification.py` — new coverage for `apply_report` as a `Mapping`, a JSON file path, a malformed mapping, and an unreadable/malformed JSON file.
- `scripts/tests/test_online_workflows.py` — new coverage proving `run_release_test` feeds a live-adapter `ApplyReport` through the real `qualify_target` validation path (P1), and new coverage for preflight `RuntimeError` translation in `run_integration`/`run_release_test` (P2, 3.3), plus two CLI-level tests.
- `scripts/teamlib/migrate.py` — add a structured `recovery` payload to `MigrationRunError` for every failure that retains the migration mutex after an attempt has started (P2, 3.2).
- `scripts/tests/test_migration_runner.py` — new coverage for the recovery-context fields across deterministic/unknown payload, verification, observation, inventory, event, attempt-state, and mutex-release failures, plus a CLI-rendering round-trip test.
- `scripts/team.py` — render `.recovery` (when present) on any caught error without discarding the original message (P2, 3.2); translate `RuntimeError` from `preflight_online` is already done here for the low-level command — no change needed there, only `online_workflows.py` needs the new translation (P2, 3.3).
- `scripts/teamlib/online_workflows.py` — translate `RuntimeError` from `deps.preflight(...)` to `OnlineWorkflowError` in both `run_integration` and `run_release_test` (P2, 3.3).
- `scripts/sql/migration_metadata.sql` — replace the flat, behaviorally-incomplete reference DDL with the exact bootstrap payload the runtime executes (DDL, backward-compatible backfill, and the identity decision tree), parameterized by two clearly documented placeholder tokens (P2, 3.4).
- `scripts/tests/test_sql_metadata_store.py` — replace the token-matching synchronization test with a byte-equality proof that the runtime payload is generated from the checked-in file, and add the missing identity-decision scenarios (empty version-1 adoption, non-empty version-1 mismatch, project-identity mismatch).

No new files are created.

---

## Task 1: Fix the P1 apply-report handoff in `qualify_target`

**Files:**
- Modify: `scripts/teamlib/qualification.py:263-361`
- Test: `scripts/tests/test_qualification.py`

**Interfaces:**
- Consumes: `teamlib.release.ApplyReport` (existing, unchanged) — `ApplyReport.as_dict()` returns the mapping shape `_check_apply_report` already validates (`version`, `status`, `source_commit`, `archive_digest`, `target_state_key`, `target_digest`, `history_digest`, `pending`).
- Produces: `qualify_target(..., apply_report: str | Path | Mapping[str, Any] | None = None, ...)` — callers may now pass a `Mapping` directly (as `run_release_test` already builds via `apply_report.as_dict()`/`dict(apply_report)`) or a path/string to a JSON file (as the `qualify-target` CLI command already does). Both enter `_check_apply_report`, which is unchanged.

### Root cause

`scripts/teamlib/qualification.py:356` calls `_check_apply_report(Path(apply_report), ...)` unconditionally. `_check_apply_report` already accepts `Mapping[str, Any] | str | Path` and internally branches on `isinstance(value, Mapping)` — but the caller wraps every value in `Path(...)` first, so a `dict` (what `run_release_test` passes) raises `TypeError: argument should be a str or an os.PathLike object ... not 'dict'` before that branch is ever reached.

- [ ] **Step 1: Write the failing tests**

Add to `scripts/tests/test_qualification.py`. First add the import at the top (alongside the existing `teamlib.qualification` import line):

```python
from teamlib.release import ApplyReport
```

Then, inside `QualificationTests`, add a helper and four tests:

```python
    def apply_report_for(self, config):
        metadata = profile_target(config, "METADATA")
        return ApplyReport(
            "applied", (), archive_digest="e" * 64, source_commit="a" * 40,
            target_state_key=metadata.state_key, target_digest="1" * 64,
            history_digest="2" * 64,
        )

    def test_qualify_target_accepts_an_apply_report_mapping_from_the_live_adapter(self):
        config = config_for()
        fake_app = AppCheckReport(
            "a" * 40, {"target_kind": "persistent", "instance_id": "INSTANCE"}, (),
            "a" * 64, "b" * 64, {"apps": ["employee"], "checks": 1, "unknown": 0}, "PASS",
        )
        manifest = SimpleNamespace(source_commit="a" * 40, archive_digest="e" * 64)
        apply_report = self.apply_report_for(config)
        with patch("teamlib.qualification.verify_candidate_apps", return_value=fake_app), \
                patch("teamlib.qualification.verify_release", return_value=manifest):
            report = qualify_target(
                self.root, config, "a" * 40, ("employee",),
                store=FakeStore(), work=self.root / "work",
                release_archive=self.root / "release.tar",
                apply_report=apply_report.as_dict(),
                runner_contract=Path("ci/runner-contract.json"),
                runtime_report=runtime_report(),
                sql_runner=lambda *args, **kwargs: object(),
            )
        self.assertEqual(report["final_status"], "PASS")
        self.assertEqual(report["archive_digest"], "e" * 64)

    def test_qualify_target_accepts_an_apply_report_json_file(self):
        config = config_for()
        fake_app = AppCheckReport(
            "a" * 40, {"target_kind": "persistent", "instance_id": "INSTANCE"}, (),
            "a" * 64, "b" * 64, {"apps": ["employee"], "checks": 1, "unknown": 0}, "PASS",
        )
        manifest = SimpleNamespace(source_commit="a" * 40, archive_digest="e" * 64)
        apply_report_path = self.root / "apply-report.json"
        apply_report_path.write_text(
            json.dumps(self.apply_report_for(config).as_dict()), encoding="utf-8",
        )
        with patch("teamlib.qualification.verify_candidate_apps", return_value=fake_app), \
                patch("teamlib.qualification.verify_release", return_value=manifest):
            report = qualify_target(
                self.root, config, "a" * 40, ("employee",),
                store=FakeStore(), work=self.root / "work2",
                release_archive=self.root / "release.tar",
                apply_report=apply_report_path,
                runner_contract=Path("ci/runner-contract.json"),
                runtime_report=runtime_report(),
                sql_runner=lambda *args, **kwargs: object(),
            )
        self.assertEqual(report["final_status"], "PASS")

    def test_qualify_target_rejects_a_malformed_apply_report_mapping(self):
        config = config_for()
        manifest = SimpleNamespace(source_commit="a" * 40, archive_digest="e" * 64)
        with patch("teamlib.qualification.verify_release", return_value=manifest):
            with self.assertRaisesRegex(QualificationError, "unexpected shape|not a successful"):
                qualify_target(
                    self.root, config, "a" * 40, ("employee",),
                    store=FakeStore(), work=self.root / "work3",
                    release_archive=self.root / "release.tar",
                    apply_report={"version": 1, "status": "pending"},
                    runner_contract=Path("ci/runner-contract.json"),
                    runtime_report=runtime_report(),
                    sql_runner=lambda *args, **kwargs: object(),
                )

    def test_qualify_target_rejects_an_unreadable_apply_report_file(self):
        config = config_for()
        manifest = SimpleNamespace(source_commit="a" * 40, archive_digest="e" * 64)
        bad_path = self.root / "bad-apply-report.json"
        bad_path.write_text("not json", encoding="utf-8")
        with patch("teamlib.qualification.verify_release", return_value=manifest):
            with self.assertRaisesRegex(QualificationError, "not valid UTF-8 JSON"):
                qualify_target(
                    self.root, config, "a" * 40, ("employee",),
                    store=FakeStore(), work=self.root / "work4",
                    release_archive=self.root / "release.tar",
                    apply_report=bad_path,
                    runner_contract=Path("ci/runner-contract.json"),
                    runtime_report=runtime_report(),
                    sql_runner=lambda *args, **kwargs: object(),
                )
```

- [ ] **Step 2: Run the new tests to verify the first two fail**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_qualification -v`
Expected: the two tests that pass a `dict` as `apply_report` — `test_qualify_target_accepts_an_apply_report_mapping_from_the_live_adapter` and `test_qualify_target_rejects_a_malformed_apply_report_mapping` — both FAIL, because `Path(apply_report)` raises `TypeError: argument should be a str or an os.PathLike object where __fspath__ returns a str, not 'dict'` before `_check_apply_report`'s own validation (and therefore before the expected `QualificationError`) ever runs. The two tests that pass a file path — `test_qualify_target_accepts_an_apply_report_json_file` and `test_qualify_target_rejects_an_unreadable_apply_report_file` — already PASS today, since `Path(str_or_path)` is a no-op-equivalent cast for those. Confirm this 2-fail/2-pass split before editing.

- [ ] **Step 3: Fix `qualify_target`**

In `scripts/teamlib/qualification.py`, change the signature (around line 272):

```python
    apply_report: str | Path | None = None,
```

to:

```python
    apply_report: str | Path | Mapping[str, Any] | None = None,
```

Then fix the call site (around line 356):

```python
        report["archive_digest"] = manifest.archive_digest
        _check_apply_report(
            Path(apply_report),
            source_commit=source_commit,
            archive_digest=manifest.archive_digest,
            target_state_key=targets["METADATA"].state_key,
        )
```

to:

```python
        report["archive_digest"] = manifest.archive_digest
        _check_apply_report(
            apply_report,
            source_commit=source_commit,
            archive_digest=manifest.archive_digest,
            target_state_key=targets["METADATA"].state_key,
        )
```

`_check_apply_report` already does `dict(value) if isinstance(value, Mapping) else _load_json(value, "apply report")[1]`, so a `str`/`Path` still resolves through `_load_json` exactly as before; only the `Mapping` branch was unreachable before this fix.

- [ ] **Step 4: Run the tests again to verify all four pass**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_qualification -v`
Expected: all four new tests PASS, and every pre-existing test in the file still PASSES.

- [ ] **Step 5: Commit**

```bash
git add scripts/teamlib/qualification.py scripts/tests/test_qualification.py
git commit -m "$(cat <<'EOF'
fix: accept apply_report mappings in qualify_target without a Path() cast

run_release_test builds apply_report as a dict from the live ApplyReport
adapter, but qualify_target unconditionally wrapped it in Path(...) before
validation, turning a successful release apply into a TypeError instead of
qualification evidence. _check_apply_report already branched on Mapping vs.
str/Path; only the caller's premature cast was wrong.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Prove `run_release_test` feeds a live `ApplyReport` through the real `qualify_target`

**Files:**
- Modify: `scripts/tests/test_online_workflows.py`

**Interfaces:**
- Consumes: `teamlib.online_workflows.run_release_test`, `teamlib.online_workflows.OnlineDependencies`, `teamlib.qualification.qualify_target` (Task 1's fixed version), `teamlib.app_checks.AppCheckReport`.
- Produces: nothing new; this is a regression test only.

Every existing `run_release_test` test replaces the `qualify_release` dependency with a hand-written stub that returns a canned dict — none of them ever call the real `qualify_target`, so Task 1's bug was invisible to this suite. This task adds one test where `qualify_release` is a thin wrapper around the *real* `qualify_target`, so the `apply_document` `run_release_test` builds from a real `ApplyReport` actually reaches `_check_apply_report`.

- [ ] **Step 1: Write the failing test**

Add to `scripts/tests/test_online_workflows.py`. Add these imports at the top, alongside the existing ones:

```python
from unittest.mock import patch
from teamlib.app_checks import AppCheckReport
from teamlib.qualification import qualify_target
```

Then add a test method inside `OnlineWorkflowTests` (it can sit right after `test_release_test_reads_live_history_and_emits_evidence_without_plan_files`):

```python
    def test_release_test_qualification_validates_a_real_apply_report_through_qualify_target(self):
        events: list[str] = []
        dependencies, manifest = self.release_dependencies(events)
        (self.root / "ci" / "app-checks").mkdir(parents=True)
        (self.root / "ci" / "app-checks" / "employee.json").write_text(
            json.dumps({"version": 1, "alias": "employee", "page_ids": [1], "checks": [
                {"id": "objects", "page_id": 1, "kind": "select", "verify_sql": "employee/objects.verify.sql",
                 "expected_objects": ["APP.T"], "sql": "SELECT 'objects' assertion_name, 'PASS' status FROM dual"},
            ]}),
            encoding="utf-8",
        )

        class FakeMetadataStore:
            def validate_observation_chain(self, target):
                return None

            def read_history(self, target):
                return {"m1": {"status": "APPLIED", "checksum": "a" * 64, "sequence": 2}}

            def read_state(self, target):
                return {"attempts": {}, "observations": [{"sequence": 3, "after": "c" * 64}]}

        def real_qualify_release(repo, config, source_commit, aliases, *, release_archive, apply_report, flow_executable, runtime_report):
            fake_app = AppCheckReport(
                source_commit, {"target_kind": "persistent"}, (), "a" * 64, "b" * 64,
                {"apps": list(aliases), "checks": 1, "unknown": 0}, "PASS",
            )
            with patch("teamlib.qualification.verify_candidate_apps", return_value=fake_app), \
                    patch("teamlib.qualification.verify_release", return_value=manifest):
                return qualify_target(
                    repo, config, source_commit, aliases,
                    store=FakeMetadataStore(), work=self.root / "qualify-work",
                    release_archive=release_archive, apply_report=apply_report,
                    flow_executable=flow_executable, runner_contract=Path("ci/runner-contract.json"),
                    runtime_report=runtime_report, sql_runner=lambda *args, **kwargs: object(),
                )

        dependencies = OnlineDependencies(**{**dependencies.__dict__, "qualify_release": real_qualify_release})
        archive = self.root / "release.tar"
        archive.write_bytes(b"verified archive")
        target = self.root / "targets" / "test.json"
        target.parent.mkdir()
        target.write_text("{}\n", encoding="utf-8")
        out = self.root / "test-evidence.json"

        result = run_release_test(
            self.root, config_for(role="test", environment="test"), archive, target, out,
            flow_executable=str(self.flow_runner), dependencies=dependencies,
        )

        self.assertEqual(result.status, "PASS")
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written["archive_digest"], manifest.archive_digest)
```

- [ ] **Step 2: Run it to see the pre-fix failure mode (optional but instructive)**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_online_workflows.OnlineWorkflowTests.test_release_test_qualification_validates_a_real_apply_report_through_qualify_target -v`
Expected (before Task 1's fix, if applied out of order): `TypeError: argument should be a str or an os.PathLike object ... not 'dict'` surfacing through `run_release_test`. If Task 1 is already applied, this test PASSES immediately — that is fine and expected; Task 1's fix makes both Step 2 here and Task 1's own tests pass together.

- [ ] **Step 3: Run the full file to confirm no regressions**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_online_workflows -v`
Expected: all tests PASS, including every pre-existing `run_release_test`/`run_integration` test.

- [ ] **Step 4: Commit**

```bash
git add scripts/tests/test_online_workflows.py
git commit -m "$(cat <<'EOF'
test: exercise run_release_test's apply-report handoff through real qualify_target

Every prior run_release_test test replaced qualify_release with a stub that
never called qualify_target, so the Path(dict) defect in qualification.py
was invisible here. This wires qualify_release to the real qualify_target
(with only SQLcl and app-check verification faked) so the conversion
run_release_test performs on the live ApplyReport is actually validated.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add structured recovery context to mutex-retaining migration failures

**Files:**
- Modify: `scripts/teamlib/migrate.py`
- Test: `scripts/tests/test_migration_runner.py`

**Interfaces:**
- Produces: `MigrationRunError(message: str, recovery: Mapping[str, Any] | None = None)`. `error.recovery` is `None` for failures that release the mutex before any attempt started, and a `dict` with keys `run_token`, `attempt_id`, `migration_id`, `operation`, `phase`, `result` (`"FAILED"` or `"UNKNOWN"`), `mutex_retained` (`True` whenever `.recovery` is set), and `evidence_path` (a string path or `None`) for every failure that retains the mutex.
- Consumed by: Task 4 (`scripts/team.py` CLI rendering) reads `getattr(exc, "recovery", None)`.

### Design

Two new module-level helpers in `migrate.py`:

```python
_EVIDENCE_LOG_RE = re.compile(r"; see (\S+)$")


def _evidence_path(exc: BaseException) -> str | None:
    """Best-effort local SQLcl log path already embedded in an exception chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        match = _EVIDENCE_LOG_RE.search(str(current))
        if match:
            return match.group(1)
        current = current.__cause__ or current.__context__
    return None


def _recovery_context(
    *,
    run_token: str,
    attempt_id: str | None,
    migration_id: str | None,
    operation: str,
    phase: str,
    result: str,
    mutex_retained: bool,
    evidence_path: str | None,
) -> dict[str, Any]:
    return {
        "run_token": run_token,
        "attempt_id": attempt_id,
        "migration_id": migration_id,
        "operation": operation,
        "phase": phase,
        "result": result,
        "mutex_retained": mutex_retained,
        "evidence_path": evidence_path,
    }
```

`SqlclError` messages already end with `"; see <log_path>"` (see `scripts/teamlib/sqlcl.py:375,379`), so `_evidence_path` recovers that path without any new plumbing; it returns `None` for failures that never touch SQLcl (e.g. a Python-level verification callback returning `False`).

- [ ] **Step 1: Write the failing tests**

Add to `scripts/tests/test_migration_runner.py`, inside `MigrationRunnerTests` (after `test_known_failure_is_recorded_and_blocks_followup`):

```python
    def test_deterministic_failure_after_attempt_start_carries_recovery_context(self):
        def fail(migration):
            raise MigrationRunError("payload failed")
        try:
            apply_plan(self.migrations, self.profiles(bootstrap=True, execute=fail))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            recovery = error.recovery
        self.assertIsNotNone(recovery)
        self.assertEqual(len(recovery["run_token"]), 32)
        self.assertEqual(recovery["migration_id"], self.migration_id)
        self.assertEqual(recovery["operation"], "migrate")
        self.assertEqual(recovery["phase"], "execute")
        self.assertEqual(recovery["result"], "FAILED")
        self.assertTrue(recovery["mutex_retained"])
        self.assertIn(recovery["attempt_id"], self.store.read_state(self.target)["attempts"])

    def test_verification_failure_wraps_without_losing_the_original_message(self):
        try:
            apply_plan(self.migrations, self.profiles(
                bootstrap=True, execute=lambda migration: None, verify=lambda migration: False,
            ))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertIn("verification failed", str(error))
            self.assertEqual(error.recovery["phase"], "verify")
            self.assertEqual(error.recovery["result"], "FAILED")

    def test_unknown_payload_failure_reports_unknown_result(self):
        def fail(migration):
            raise SqlclError("SQLcl timed out; target state is unknown")
        try:
            apply_plan(self.migrations, self.profiles(bootstrap=True, execute=fail))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertEqual(error.recovery["result"], "UNKNOWN")
            self.assertEqual(error.recovery["phase"], "execute")

    def test_observation_failure_after_attempt_start_carries_recovery_context(self):
        def observe(migration, phase):
            if phase == "after":
                raise MigrationRunError("boom")
            return None

        try:
            apply_plan(self.migrations, self.profiles(
                bootstrap=True, execute=lambda migration: None, verify=lambda migration: True,
                observe=observe, require_observation=False,
            ))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertEqual(error.recovery["phase"], "observe-after")
            self.assertEqual(error.recovery["result"], "FAILED")

    def test_inventory_failure_after_attempt_start_carries_recovery_context(self):
        inventory = self.inventory(
            [{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "stable"}]
        )

        class FailSecondRecordInventoryStore(MigrationStore):
            def __init__(self, root):
                super().__init__(root)
                self._record_inventory_calls = 0

            def record_inventory(self, store_target, inventory, *, run_token):
                self._record_inventory_calls += 1
                if self._record_inventory_calls == 2:
                    raise MigrationRunError("inventory write failed")
                return super().record_inventory(store_target, inventory, run_token=run_token)

        store = FailSecondRecordInventoryStore(self.root / "inventory-failure-state")
        try:
            apply_plan(
                self.migrations,
                {
                    **self.profiles(),
                    "store": store,
                    "bootstrap": True,
                    "execute": lambda *_args: None,
                    "verify": lambda *_args: True,
                    "observe": lambda *_args: inventory,
                    "require_observation": True,
                },
            )
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertEqual(error.recovery["phase"], "record-inventory")
            self.assertEqual(error.recovery["result"], "FAILED")
            self.assertTrue(error.recovery["mutex_retained"])

    def test_pre_attempt_failure_does_not_claim_recovery_is_required(self):
        try:
            apply_plan(self.migrations, self.profiles(
                bootstrap=True, execute=lambda migration: None, verify=lambda migration: True,
                observe=lambda migration, phase: None, require_observation=True,
            ))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertIsNone(error.recovery)
```

Also extend the existing `test_committed_event_with_lost_ack_is_unknown_without_failed_rewrite` test (the "event acknowledgement" unknown case) to assert on `recovery` — change its `with self.assertRaisesRegex(MigrationRunError, "unknown"): apply_plan(...)` block to:

```python
        try:
            apply_plan(
                self.migrations,
                {
                    **self.profiles(),
                    "store": store,
                    "bootstrap": True,
                    "execute": lambda *_args: None,
                    "verify": lambda *_args: True,
                    "observe": lambda *_args: inventory,
                    "require_observation": True,
                },
            )
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertRegex(str(error), "unknown")
            self.assertEqual(error.recovery["phase"], "record-event")
            self.assertEqual(error.recovery["result"], "UNKNOWN")
```

(keep the rest of that test — the `state = store.read_state(...)` assertions after it — unchanged).

Also add a subclass-based test for the attempt-state-unknown and mutex-release-unknown branches by extending the two existing tests (`test_unknown_attempt_state_update_does_not_release_mutex`, `test_unknown_mutex_release_keeps_completed_evidence`) to assert on `recovery` instead of adding brand-new tests — append these two assertions to the existing `except`/`assertRaisesRegex` blocks by switching them from `with self.assertRaisesRegex(...)` to a `try/except` that also inspects `.recovery`:

```python
    def test_unknown_attempt_state_update_does_not_release_mutex(self):
        inventory = self.inventory(
            [{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "stable"}]
        )

        class LoseAttemptStateAckStore(MigrationStore):
            def record_attempt_state(self, *args, **kwargs):
                try:
                    raise SqlclError("SQLcl timed out; target state is unknown")
                except SqlclError as exc:
                    raise MigrationStoreError("attempt state result unavailable") from exc

        store = LoseAttemptStateAckStore(self.root / "attempt-state-lost-ack")
        try:
            apply_plan(
                self.migrations,
                {
                    **self.profiles(),
                    "store": store,
                    "bootstrap": True,
                    "execute": lambda *_args: (_ for _ in ()).throw(MigrationRunError("known payload failure")),
                    "verify": lambda *_args: True,
                    "observe": lambda *_args: inventory,
                    "require_observation": True,
                },
            )
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertRegex(str(error), "attempt-state result is unknown")
            self.assertEqual(error.recovery["phase"], "record-attempt-state")
            self.assertEqual(error.recovery["result"], "UNKNOWN")
        state = store.read_state(self.target)
        self.assertEqual(
            {attempt["state"] for attempt in state["attempts"].values()},
            {"RUNNING"},
        )
        self.assertTrue(state["mutex"]["owner_token"])

    def test_unknown_mutex_release_keeps_completed_evidence(self):
        inventory = self.inventory(
            [{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "stable"}]
        )

        class LoseReleaseAckStore(MigrationStore):
            def release(self, *args, **kwargs):
                try:
                    raise SqlclError("SQLcl timed out; target state is unknown")
                except SqlclError as exc:
                    raise MigrationStoreError("mutex release result unavailable") from exc

        store = LoseReleaseAckStore(self.root / "release-lost-ack")
        try:
            apply_plan(
                self.migrations,
                {
                    **self.profiles(),
                    "store": store,
                    "bootstrap": True,
                    "execute": lambda *_args: None,
                    "verify": lambda *_args: True,
                    "observe": lambda *_args: inventory,
                    "require_observation": True,
                },
            )
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertRegex(str(error), "mutex release is unknown")
            self.assertEqual(error.recovery["phase"], "release")
            self.assertEqual(error.recovery["result"], "UNKNOWN")
            self.assertTrue(error.recovery["mutex_retained"])
        state = store.read_state(self.target)
        self.assertEqual(
            {attempt["state"] for attempt in state["attempts"].values()},
            {"APPLIED"},
        )
        self.assertTrue(state["mutex"]["owner_token"])
```

These two replace the existing `test_unknown_attempt_state_update_does_not_release_mutex` and `test_unknown_mutex_release_keeps_completed_evidence` methods in place (same name, same original assertions retained, `recovery` assertions added).

Also add `SqlclError` to the imports already present at the top of the file (`from teamlib.sqlcl import SqlclError` is already imported — confirm it, no change needed there).

- [ ] **Step 2: Run the new/changed tests to verify they fail**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_migration_runner -v`
Expected: every test referencing `.recovery` FAILS with `AttributeError: 'MigrationRunError' object has no attribute 'recovery'`. All untouched tests still PASS.

- [ ] **Step 3: Implement the recovery context in `migrate.py`**

Add `import re` to the top-level imports (alongside the existing `import hashlib`, `import inspect`).

Change the exception class (around line 26-27):

```python
class MigrationRunError(RuntimeError):
    pass
```

to:

```python
class MigrationRunError(RuntimeError):
    """Raised for a fail-closed migration outcome.

    ``recovery`` is populated only when the failure retains the migration
    mutex: it carries what ``recover-migration`` needs (the run token) plus
    enough context to explain why recovery is required. It never includes a
    secret, credential, wallet, or connection string.
    """

    def __init__(self, message: str, recovery: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.recovery = dict(recovery) if isinstance(recovery, Mapping) else None
```

Add the two helpers from the Design section above, placed right after the `_report(...)` function and before `_apply_operation(...)`.

Replace the per-migration exception handler (the `except Exception as exc:` block inside the `for migration_id in selected_ids:` loop, currently):

```python
            except Exception as exc:
                unknown = result_is_unknown(exc)
                state = "UNKNOWN" if unknown else "FAILED"
                if not (unknown and phase == "record-event"):
                    try:
                        store.record_attempt_state(
                            target,
                            attempt_id,
                            run_token,
                            state,
                            hashlib.sha256(str(exc).encode()).hexdigest(),
                        )
                    except Exception as state_exc:
                        if result_is_unknown(state_exc):
                            raise MigrationRunError(
                                f"migration attempt-state result is unknown: {migration_id}"
                            ) from state_exc
                        raise
                if unknown:
                    raise MigrationRunError(
                        f"migration result is unknown during {phase}: {migration_id}: {exc}"
                    ) from exc
                if isinstance(exc, MigrationRunError):
                    raise
                raise MigrationRunError(
                    f"migration payload failed during {phase}: {migration_id}: {exc}"
                ) from exc
```

with:

```python
            except Exception as exc:
                unknown = result_is_unknown(exc)
                state = "UNKNOWN" if unknown else "FAILED"
                if not (unknown and phase == "record-event"):
                    try:
                        store.record_attempt_state(
                            target,
                            attempt_id,
                            run_token,
                            state,
                            hashlib.sha256(str(exc).encode()).hexdigest(),
                        )
                    except Exception as state_exc:
                        if result_is_unknown(state_exc):
                            raise MigrationRunError(
                                f"migration attempt-state result is unknown: {migration_id}",
                                _recovery_context(
                                    run_token=run_token, attempt_id=attempt_id,
                                    migration_id=migration_id, operation=action,
                                    phase="record-attempt-state", result="UNKNOWN",
                                    mutex_retained=True, evidence_path=_evidence_path(state_exc),
                                ),
                            ) from state_exc
                        raise
                recovery = _recovery_context(
                    run_token=run_token, attempt_id=attempt_id, migration_id=migration_id,
                    operation=action, phase=phase, result=state,
                    mutex_retained=True, evidence_path=_evidence_path(exc),
                )
                if unknown:
                    raise MigrationRunError(
                        f"migration result is unknown during {phase}: {migration_id}: {exc}",
                        recovery,
                    ) from exc
                if isinstance(exc, MigrationRunError):
                    raise MigrationRunError(str(exc), recovery) from exc
                raise MigrationRunError(
                    f"migration payload failed during {phase}: {migration_id}: {exc}",
                    recovery,
                ) from exc
```

Replace the mutex-release exception handler (near the end of `_apply_operation`, currently):

```python
        try:
            store.release(target, run_token)
        except Exception as exc:
            if result_is_unknown(exc):
                raise MigrationRunError(
                    f"migration mutex release is unknown for run {run_token}; "
                    "inspect live metadata before recovery"
                ) from exc
            raise MigrationRunError(
                f"migration mutex release failed for run {run_token}: {exc}"
            ) from exc
```

with:

```python
        try:
            store.release(target, run_token)
        except Exception as exc:
            unknown = result_is_unknown(exc)
            recovery = _recovery_context(
                run_token=run_token, attempt_id=None, migration_id=None, operation=action,
                phase="release", result="UNKNOWN" if unknown else "FAILED",
                mutex_retained=True, evidence_path=_evidence_path(exc),
            )
            if unknown:
                raise MigrationRunError(
                    f"migration mutex release is unknown for run {run_token}; "
                    "inspect live metadata before recovery",
                    recovery,
                ) from exc
            raise MigrationRunError(
                f"migration mutex release failed for run {run_token}: {exc}",
                recovery,
            ) from exc
```

Leave every other `raise MigrationRunError(...)` in the file untouched (pre-attempt-start failures, production refusal, malformed profiles, etc.) — those correctly leave `.recovery` as `None`.

- [ ] **Step 4: Run the tests again to verify they pass**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_migration_runner -v`
Expected: all tests PASS, including the pre-existing `assertRaisesRegex` message checks (message text is unchanged; only a second constructor argument was added).

- [ ] **Step 5: Run the full offline suite to check for cross-file regressions**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v`
Expected: all tests PASS (in particular `test_production_boundary.py`, which also raises/catches `MigrationRunError`).

- [ ] **Step 6: Commit**

```bash
git add scripts/teamlib/migrate.py scripts/tests/test_migration_runner.py
git commit -m "$(cat <<'EOF'
fix: surface structured recovery context on mutex-retaining migration failures

Once an attempt starts, deterministic and unknown failures correctly kept
the migration mutex, but several paths (an existing MigrationRunError
re-raised unchanged, attempt-state update, mutex release) exposed only the
underlying message. recover-migration needs the run token positionally;
without it in the failure output an operator has no reliable way to invoke
recovery. MigrationRunError now carries an optional structured `recovery`
payload (run token, attempt id, migration id/operation, phase, FAILED/
UNKNOWN classification, mutex-retained flag, best-effort SQLcl log path)
whenever the mutex is retained, and stays None otherwise.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Render recovery context in the CLI without losing the original diagnostic

**Files:**
- Modify: `scripts/team.py:784-786`
- Test: `scripts/tests/test_migration_runner.py`

**Interfaces:**
- Consumes: `exc.recovery` from Task 3.
- Produces: on any caught error with a non-`None` `.recovery`, `team.main()` prints the original message line (unchanged) to stderr, then a second line `json.dumps({"recovery": {...}})` to stderr, then returns the same exit code as before (3, since `MigrationRunError` is not a `ConfigError`).

- [ ] **Step 1: Write the failing test**

Add to `scripts/tests/test_migration_runner.py` (needs `import io`, `import contextlib`, and `from unittest.mock import patch` — add these to the top-of-file imports if not already present; `unittest.mock.patch` is already imported):

```python
    def test_cli_prints_recovery_context_an_operator_can_feed_to_recover_migration(self):
        def fail(migration):
            raise MigrationRunError("payload failed")
        try:
            apply_plan(self.migrations, self.profiles(bootstrap=True, execute=fail))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            real_error = error
        self.assertIsNotNone(real_error.recovery)

        stderr = io.StringIO()
        with patch("team._online", side_effect=real_error):
            with contextlib.redirect_stderr(stderr):
                code = team.main(["doctor"])
        self.assertEqual(code, 3)

        lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
        self.assertEqual(lines[0], str(real_error))
        rendered = json.loads(lines[1])
        self.assertEqual(rendered["recovery"], real_error.recovery)

        parsed = team._parser().parse_args([
            "recover-migration", rendered["recovery"]["run_token"], "--evidence", "evidence.json",
        ])
        self.assertEqual(parsed.run_token, real_error.recovery["run_token"])
```

Add `import io` and `import contextlib` and `import json` to the top of `scripts/tests/test_migration_runner.py` if any are missing (check current imports first — `json` is likely already absent from this file; add it).

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_migration_runner.MigrationRunnerTests.test_cli_prints_recovery_context_an_operator_can_feed_to_recover_migration -v`
Expected: FAILS — `lines` has only one element (`IndexError`) because `team.main()` currently prints just the message line.

- [ ] **Step 3: Update `team.py`'s error rendering**

In `scripts/team.py`, change the bottom of `main()` (around line 784-786) from:

```python
    except (ConfigError, ControlStoreError, StateError, PatchError, ApexError, MigrationRunError, MigrationStoreError, DeployError, ReleaseError, ReleaseAdapterError, RunbookError, CIError, AppCheckError, QualificationError, TreeError, BundleError, InventoryError, OnlineWorkflowError) as exc:
        print(str(exc), file=sys.stderr)
        return 2 if isinstance(exc, ConfigError) else 3
```

to:

```python
    except (ConfigError, ControlStoreError, StateError, PatchError, ApexError, MigrationRunError, MigrationStoreError, DeployError, ReleaseError, ReleaseAdapterError, RunbookError, CIError, AppCheckError, QualificationError, TreeError, BundleError, InventoryError, OnlineWorkflowError) as exc:
        print(str(exc), file=sys.stderr)
        recovery = getattr(exc, "recovery", None)
        if isinstance(recovery, dict):
            print(json.dumps({"recovery": recovery}, sort_keys=True), file=sys.stderr)
        return 2 if isinstance(exc, ConfigError) else 3
```

`json` is already imported at the top of `team.py`.

- [ ] **Step 4: Run the test again to verify it passes**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_migration_runner -v`
Expected: all tests PASS.

- [ ] **Step 5: Run the full offline suite**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v`
Expected: all tests PASS. (Only `MigrationRunError` ever sets `.recovery`, so no other caught exception type changes behavior.)

- [ ] **Step 6: Commit**

```bash
git add scripts/team.py scripts/tests/test_migration_runner.py
git commit -m "$(cat <<'EOF'
feat: render migration recovery context on the CLI without losing the diagnostic

team.main() printed only the exception message on failure, so the recovery
context Task 3 now attaches to mutex-retaining MigrationRunErrors had no way
to reach an operator's terminal. main() now appends one JSON line carrying
the recovery payload (run token, attempt id, phase, result, evidence path)
whenever an exception carries one, immediately after the unchanged message
line.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Translate online-preflight `RuntimeError` at the workflow boundary

**Files:**
- Modify: `scripts/teamlib/online_workflows.py:371-504`
- Test: `scripts/tests/test_online_workflows.py`

**Interfaces:**
- Consumes: `preflight_online` (and any injected `OnlineDependencies.preflight`) — declared to raise `RuntimeError` for missing/invalid runtime dependencies (`scripts/teamlib/runtime.py`).
- Produces: `run_integration` and `run_release_test` now raise `OnlineWorkflowError` (not `RuntimeError`) when `deps.preflight(...)` raises `RuntimeError`, preserving the original message and chaining the cause (`from exc`).

- [ ] **Step 1: Write the failing tests**

Add to `scripts/tests/test_online_workflows.py`, inside `OnlineWorkflowTests`:

```python
    def test_run_integration_translates_preflight_runtime_errors(self):
        for message in (
            "SQLcl executable is unavailable",
            "TEAM_FLOW_RUNNER is required for declared flow checks",
            "profile identity probe does not match TABLES",
            "observed sqlcl version 25.1 does not satisfy required 26.2.1+",
        ):
            with self.subTest(message=message):
                events: list[str] = []

                def failing_preflight(_config, _repo, _flow, _message=message):
                    events.append("preflight")
                    raise RuntimeError(_message)

                dependencies, _ = self.dependencies(events)
                dependencies = OnlineDependencies(**{**dependencies.__dict__, "preflight": failing_preflight})
                with self.assertRaisesRegex(OnlineWorkflowError, re.escape(message)):
                    run_integration(
                        self.root, config_for(), self.root / "out.json",
                        flow_executable=str(self.flow_runner), dependencies=dependencies,
                    )
                self.assertEqual(events, ["preflight"])

    def test_run_release_test_translates_preflight_runtime_errors(self):
        events: list[str] = []

        def failing_preflight(_config, _repo, _flow):
            events.append("preflight")
            raise RuntimeError("JDK executable is unavailable")

        dependencies, manifest = self.release_dependencies(events)
        dependencies = OnlineDependencies(**{**dependencies.__dict__, "preflight": failing_preflight})
        archive = self.root / "release.tar"
        archive.write_bytes(b"verified archive")
        target = self.root / "targets" / "test.json"
        target.parent.mkdir()
        target.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(OnlineWorkflowError, "JDK executable is unavailable"):
            run_release_test(
                self.root, config_for(role="test", environment="test"), archive, target,
                self.root / "out.json", flow_executable=str(self.flow_runner), dependencies=dependencies,
            )
        self.assertEqual(events, ["verify-archive", "preflight"])
```

Add `import re` to the top of `scripts/tests/test_online_workflows.py` if not already present (it is not, currently).

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_online_workflows.OnlineWorkflowTests.test_run_integration_translates_preflight_runtime_errors scripts.tests.test_online_workflows.OnlineWorkflowTests.test_run_release_test_translates_preflight_runtime_errors -v`
Expected: both FAIL — the bare `RuntimeError` propagates uncaught instead of being wrapped in `OnlineWorkflowError` (`assertRaisesRegex` reports the wrong exception type).

- [ ] **Step 3: Wrap the preflight calls in `online_workflows.py`**

In `run_integration`, change:

```python
    aliases = tuple(sorted(config.apps))
    runtime = deps.preflight(config, repo_path, flow_executable)
```

to:

```python
    aliases = tuple(sorted(config.apps))
    try:
        runtime = deps.preflight(config, repo_path, flow_executable)
    except RuntimeError as exc:
        raise OnlineWorkflowError(str(exc)) from exc
```

In `run_release_test`, change:

```python
    runtime = deps.preflight(config, repo_path, flow_executable)
    apply_report = deps.apply_release_live(
```

to:

```python
    try:
        runtime = deps.preflight(config, repo_path, flow_executable)
    except RuntimeError as exc:
        raise OnlineWorkflowError(str(exc)) from exc
    apply_report = deps.apply_release_live(
```

The `try` blocks wrap only the `deps.preflight(...)` call itself — nothing else changes scope, so a programming defect anywhere else in either function still propagates as whatever exception it actually is.

- [ ] **Step 4: Run the tests again to verify they pass**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_online_workflows -v`
Expected: all tests PASS.

- [ ] **Step 5: Add CLI-level tests proving no traceback escapes**

Add a new test class to `scripts/tests/test_online_workflows.py` (needs `import team` and `from unittest.mock import patch` at the top — add `import team` if not present):

```python
class PreflightCliTranslationTests(unittest.TestCase):
    ENV_TEMPLATE = """\
PROJECT_NAME=team-template
TARGET_ROLE={role}
DB_ENVIRONMENT={environment}
APEX_APPS=employee:101
TABLES_SCHEMA=APP_DATA
CODE_SCHEMA=APP_CODE
APEX_PARSING_SCHEMA=APP
METADATA_SCHEMA=APP_META
APP_OWNERSHIP_MODE=shared
APEX_WORKSPACE_ID=5402650006222933
{profiles}
"""
    PROFILE_BLOCK = """\
{name}_SQLCL_CONNECTION=docker-demo
{name}_EXPECTED_USER=DEMO
{name}_EXPECTED_CURRENT_SCHEMA=DEMO
{name}_EXPECTED_DB_NAME=FREEPDB1
{name}_EXPECTED_SERVICE=freep1
{name}_EXPECTED_INSTANCE_ID=FREEPDB1
"""

    def env_file(self, root: Path, *, role: str, environment: str) -> Path:
        profiles = "".join(self.PROFILE_BLOCK.format(name=name) for name in ("TABLES", "CODE", "APEX", "METADATA", "VERIFY"))
        env_path = root / ".env"
        env_path.write_text(
            self.ENV_TEMPLATE.format(role=role, environment=environment, profiles=profiles),
            encoding="utf-8",
        )
        return env_path

    def test_cli_run_integration_rejects_preflight_failure_without_a_traceback(self):
        with tempfile.TemporaryDirectory(prefix="team-preflight-cli-") as directory:
            root = Path(directory)
            env_path = self.env_file(root, role="integration", environment="staging")
            with patch("teamlib.online_workflows.preflight_online", side_effect=RuntimeError("SQLcl executable is unavailable")):
                code = team.main(["--env", str(env_path), "run-integration", "--out", str(root / "out.json")])
        self.assertEqual(code, 3)

    def test_cli_run_release_test_rejects_preflight_failure_without_a_traceback(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory(prefix="team-preflight-cli-") as directory:
            root = Path(directory)
            env_path = self.env_file(root, role="test", environment="test")
            archive = root / "release.tar"
            archive.write_bytes(b"not a real archive")
            fake_manifest = SimpleNamespace(app_tree_digests={"employee": "c" * 64}, source_commit="a" * 40)
            with patch("teamlib.online_workflows.verify_release", return_value=fake_manifest), \
                    patch("teamlib.online_workflows.preflight_online", side_effect=RuntimeError("JDK executable is unavailable")):
                code = team.main([
                    "--env", str(env_path), "run-release-test", str(archive),
                    "--target", str(root / "target.json"), "--out", str(root / "out.json"),
                ])
        self.assertEqual(code, 3)
```

Add `import tempfile` to the top of `scripts/tests/test_online_workflows.py` if not already present (it is already imported).

- [ ] **Step 6: Run the new CLI tests**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_online_workflows.PreflightCliTranslationTests -v`
Expected: both PASS with exit code 3 and no exception escaping `team.main()`. The release-test case patches `verify_release` so its manifest check passes and execution actually reaches the patched `preflight_online` before failing.

- [ ] **Step 7: Run the full offline suite**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v`
Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add scripts/teamlib/online_workflows.py scripts/tests/test_online_workflows.py
git commit -m "$(cat <<'EOF'
fix: translate online-preflight RuntimeError to OnlineWorkflowError

preflight_online raises a bare RuntimeError for a missing SQLcl/flow
adapter, an incomplete profile, a target identity mismatch, or an
unsupported runtime version. The low-level qualify-target command already
translated that into a normal refusal, but run_integration and
run_release_test called preflight directly and let RuntimeError escape
team.py's catch list as an unhandled traceback with exit status 1. Both
now translate RuntimeError from the preflight call specifically (not from
anywhere else in the workflow) into OnlineWorkflowError, preserving the
message and exception cause.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Make `scripts/sql/migration_metadata.sql` the executable bootstrap source

**Files:**
- Modify: `scripts/sql/migration_metadata.sql` (full rewrite)
- Modify: `scripts/tests/test_sql_metadata_store.py`

**Interfaces:**
- No Python signatures change. `scripts/teamlib/migration_store.py`'s `_MIGRATION_BOOTSTRAP_SQL` constant and `SqlMigrationStore.bootstrap()` are **not modified** — the fix is entirely in the checked-in reference file and its test, which now prove byte-for-byte equivalence with the runtime payload instead of asserting on scattered tokens.

### Design

The current `scripts/sql/migration_metadata.sql` is flat `CREATE TABLE` DDL with no identity-decision logic, so it silently drifted from `_MIGRATION_BOOTSTRAP_SQL` + the dynamic identity block `SqlMigrationStore.bootstrap()` appends (project-row creation, empty-version-1 adoption, non-empty-mismatch refusal, mutex-row seeding). Rather than hand-copy that logic into the reference file (which is exactly how it drifted the first time), replace the reference file with the *exact* text the runtime executes, with the two call-time-dynamic SQL literals (`store_target.project`, `schema_set_digest`) replaced by two clearly named, documented placeholder tokens: `__PROJECT_ID_LITERAL__` and `__SCHEMA_SET_DIGEST_LITERAL__`. A new test renders the runtime's actual `bootstrap()` payload for known inputs and asserts it equals the reference file with those two tokens substituted for the same inputs' quoted literals — so any future edit to either side that breaks the correspondence fails CI immediately.

- [ ] **Step 1: Write the failing test**

In `scripts/tests/test_sql_metadata_store.py`, replace `test_embedded_and_reference_ddl_define_metadata_v2` (the whole method, lines 99-113) with:

```python
    def test_reference_sql_is_the_exact_runtime_bootstrap_payload(self):
        reference = (Path(__file__).resolve().parents[2] / "scripts" / "sql" / "migration_metadata.sql").read_text(encoding="utf-8")
        rendered_reference = (
            reference
            .replace("__PROJECT_ID_LITERAL__", "'" + self.target.project + "'")
            .replace("__SCHEMA_SET_DIGEST_LITERAL__", "'" + "a" * 64 + "'")
        )
        store = SqlMigrationStore(self.target, runner=self.runner, work_root=self.root)
        store._read_rows = lambda *args, **kwargs: [["0"]]  # type: ignore[method-assign]
        store.bootstrap(self.target, schema_set_digest="a" * 64)
        payload = self.calls[-1][1]
        self.assertEqual(payload, rendered_reference)

    def test_empty_version_one_digest_is_adopted_without_refusal(self):
        store = SqlMigrationStore(self.target, runner=self.runner, work_root=self.root)

        def read_rows(_target, _payload, prefix):
            return [["1"]] if prefix == "TEAM_META_TABLE|" else [["1", ""]]

        store._read_rows = read_rows  # type: ignore[method-assign]
        store.bootstrap(self.target, schema_set_digest="a" * 64)
        self.assertEqual([operation for operation, _ in self.calls], ["write"])

    def test_non_empty_version_one_digest_mismatch_refuses_before_metadata_write(self):
        store = SqlMigrationStore(self.target, runner=self.runner, work_root=self.root)
        reads: list[str] = []

        def read_rows(_target, _payload, prefix):
            reads.append(prefix)
            return [["1"]] if prefix == "TEAM_META_TABLE|" else [["1", "b" * 64]]

        store._read_rows = read_rows  # type: ignore[method-assign]
        with self.assertRaisesRegex(MigrationStoreError, "different project/schema set"):
            store.bootstrap(self.target, schema_set_digest="a" * 64)
        self.assertEqual(reads, ["TEAM_META_TABLE|", "TEAM_META_ID|"])
        self.assertEqual(self.calls, [])

    def test_project_identity_mismatch_refuses_before_any_read_or_write(self):
        other = Target(
            project="other-project", role="developer", environment="development",
            connection="meta", instance_id="FREE", db_name="FREEPDB1", service="freep1",
            session_user="META", current_schema="META", alias=None, workspace_id=None,
            app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="b" * 64,
        )
        store = SqlMigrationStore(self.target, runner=self.runner, work_root=self.root)
        with self.assertRaisesRegex(MigrationStoreError, "does not match the configured SQL controller"):
            store.bootstrap(other, schema_set_digest="a" * 64)
        self.assertEqual(self.calls, [])
```

`test_bootstrap_and_mutex_use_sqlcl_and_nowait_transition` already covers fresh creation (existing `None`), and `test_matching_existing_schema_set_reaches_metadata_write` already covers idempotent matching version-2 bootstrap, and `test_existing_schema_set_mismatch_refuses_before_metadata_write` already covers the version-2 mismatch case — all three are kept unchanged.

- [ ] **Step 2: Run the tests to verify the new ones fail**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_sql_metadata_store -v`
Expected: `test_reference_sql_is_the_exact_runtime_bootstrap_payload` FAILS (current reference file is flat DDL, nowhere near the rendered runtime payload). `test_empty_version_one_digest_is_adopted_without_refusal` and `test_project_identity_mismatch_refuses_before_any_read_or_write` likely already PASS (they test existing, unchanged Python behavior) — confirm which pass/fail before editing. `test_non_empty_version_one_digest_mismatch_refuses_before_metadata_write` should already PASS too.

- [ ] **Step 3: Rewrite `scripts/sql/migration_metadata.sql`**

Replace the entire file with:

```sql
-- Controller-owned migration metadata. Execute only through METADATA.
--
-- This file is the single canonical source for the migration-metadata
-- bootstrap: teamlib.migration_store.SqlMigrationStore.bootstrap() builds its
-- runtime payload from this exact text, substituting the two placeholder
-- tokens below with sql_literal()-quoted values. Change the bootstrap
-- contract here or in migration_store.py and update both together;
-- scripts/tests/test_sql_metadata_store.py fails if they diverge.
--
-- To run this file by hand for diagnosis or recovery, replace both
-- placeholders with your own quoted string literals, e.g. __PROJECT_ID_LITERAL__
-- becomes 'team-template' and __SCHEMA_SET_DIGEST_LITERAL__ becomes a quoted
-- lowercase 64-character schema-set SHA-256.

-- Bootstrap identity contract: a fresh project row records the supplied
-- lowercase schema-set SHA-256 at version 2; an upgraded version-1 row may
-- fill only its empty digest; a non-empty version-2 digest is immutable and
-- any mismatch must be refused before metadata writes begin.
DECLARE
  PROCEDURE create_if_missing(p_sql CLOB) IS
  BEGIN
    EXECUTE IMMEDIATE p_sql;
  EXCEPTION
    WHEN OTHERS THEN
      IF SQLCODE != -955 THEN RAISE; END IF;
  END;
  v_count NUMBER;
  v_pk_count NUMBER;
  v_sequence_pk NUMBER;
  v_id_pk NUMBER;
  v_pk_name VARCHAR2(128);
  v_condition VARCHAR2(4000);
  v_duplicate NUMBER;
BEGIN
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_META (
    version_number NUMBER(10) NOT NULL,
    project_id VARCHAR2(128) NOT NULL,
    schema_set_digest VARCHAR2(64) NOT NULL,
    CONSTRAINT team_migration_meta_pk PRIMARY KEY (version_number, project_id)
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_MUTEX (
    singleton_id NUMBER(1) NOT NULL,
    owner_token VARCHAR2(128),
    worker_identity VARCHAR2(256),
    host VARCHAR2(512),
    acquired_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT team_migration_mutex_pk PRIMARY KEY (singleton_id),
    CONSTRAINT team_migration_mutex_singleton_ck CHECK (singleton_id = 1)
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_HISTORY (
    id VARCHAR2(128) NOT NULL,
    operation VARCHAR2(4) NOT NULL,
    checksum VARCHAR2(64) NOT NULL,
    target VARCHAR2(16) NOT NULL,
    dependencies_json CLOB NOT NULL,
    payload_manifest_json CLOB NOT NULL,
    source_commit VARCHAR2(128) NOT NULL,
    applied_sequence NUMBER(19) NOT NULL,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL,
    applied_by VARCHAR2(256) NOT NULL,
    run_token VARCHAR2(128),
    attempt_id VARCHAR2(128),
    CONSTRAINT team_migration_history_pk PRIMARY KEY (applied_sequence),
    CONSTRAINT team_migration_history_operation_ck CHECK (operation IN ('up','down')),
    CONSTRAINT team_migration_history_target_ck CHECK (target IN ('tables', 'code'))
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_ATTEMPT (
    attempt_id VARCHAR2(128) NOT NULL,
    migration_id VARCHAR2(128) NOT NULL,
    checksum VARCHAR2(64) NOT NULL,
    action VARCHAR2(16) NOT NULL,
    state VARCHAR2(16) NOT NULL,
    run_token VARCHAR2(128) NOT NULL,
    worker_identity VARCHAR2(256) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    diagnostic_digest VARCHAR2(64),
    confirmation_digest VARCHAR2(64),
    CONSTRAINT team_migration_attempt_pk PRIMARY KEY (attempt_id),
    CONSTRAINT team_migration_attempt_action_ck CHECK (action IN ('migrate','undo','redo')),
    CONSTRAINT team_migration_attempt_state_ck CHECK (state IN ('RUNNING','APPLIED','FAILED','UNKNOWN','RECOVERED'))
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_INVENTORY (
    inventory_digest VARCHAR2(64) NOT NULL,
    manifest_json CLOB NOT NULL,
    schema_set_digest VARCHAR2(64) NOT NULL,
    normalizer_version VARCHAR2(32) NOT NULL,
    coverage_version VARCHAR2(32) NOT NULL,
    CONSTRAINT team_migration_inventory_pk PRIMARY KEY (inventory_digest)
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_OBSERVATION (
    sequence_number NUMBER(19) NOT NULL,
    migration_id VARCHAR2(128),
    attempt_id VARCHAR2(128),
    predecessor_sequence NUMBER(19),
    before_digest VARCHAR2(64) NOT NULL,
    after_digest VARCHAR2(64) NOT NULL,
    evidence_digest VARCHAR2(64) NOT NULL,
    CONSTRAINT team_migration_observation_pk PRIMARY KEY (sequence_number)
  )]');

  -- Existing v1 history receives its direction before constraints are checked.
  SELECT COUNT(*) INTO v_count FROM user_tab_columns
   WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND column_name = 'OPERATION';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY ADD (operation VARCHAR2(4))';
  END IF;
  UPDATE TEAM_MIGRATION_HISTORY SET operation = 'up' WHERE operation IS NULL;
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_HISTORY
   WHERE operation IS NULL OR operation NOT IN ('up', 'down');
  IF v_count <> 0 THEN
    RAISE_APPLICATION_ERROR(-20021, 'MIGRATION_HISTORY_OPERATION_INVALID');
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_tab_columns
   WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND column_name = 'OPERATION'
     AND data_type = 'VARCHAR2' AND data_length = 4 AND nullable = 'N';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY MODIFY (operation VARCHAR2(4) NOT NULL)';
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_constraints
   WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND constraint_name = 'TEAM_MIGRATION_HISTORY_OPERATION_CK';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY ADD CONSTRAINT team_migration_history_operation_ck CHECK (operation IN (''up'',''down''))';
  ELSE
    SELECT DBMS_LOB.SUBSTR(search_condition, 4000, 1) INTO v_condition
      FROM user_constraints
     WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND constraint_name = 'TEAM_MIGRATION_HISTORY_OPERATION_CK';
    IF REGEXP_REPLACE(UPPER(v_condition), '[[:space:]]', '') <> 'OPERATIONIN(''UP'',''DOWN'')' THEN
      RAISE_APPLICATION_ERROR(-20022, 'MIGRATION_HISTORY_OPERATION_CONSTRAINT_INVALID');
    END IF;
  END IF;

  SELECT COUNT(*) - COUNT(DISTINCT applied_sequence) INTO v_duplicate FROM TEAM_MIGRATION_HISTORY;
  IF v_duplicate <> 0 THEN
    RAISE_APPLICATION_ERROR(-20023, 'MIGRATION_HISTORY_SEQUENCE_DUPLICATE');
  END IF;
  SELECT COUNT(*) INTO v_pk_count FROM user_constraints
   WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND constraint_type = 'P';
  SELECT COUNT(*) INTO v_sequence_pk
    FROM user_constraints c JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name
   WHERE c.table_name = 'TEAM_MIGRATION_HISTORY' AND c.constraint_type = 'P'
     AND cc.column_name = 'APPLIED_SEQUENCE';
  SELECT COUNT(*) INTO v_id_pk
    FROM user_constraints c JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name
   WHERE c.table_name = 'TEAM_MIGRATION_HISTORY' AND c.constraint_type = 'P'
     AND cc.column_name = 'ID';
  IF v_pk_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY ADD CONSTRAINT team_migration_history_pk PRIMARY KEY (applied_sequence)';
  ELSIF v_sequence_pk = 0 THEN
    IF v_pk_count <> 1 OR v_id_pk <> 1 THEN
      RAISE_APPLICATION_ERROR(-20024, 'MIGRATION_HISTORY_PRIMARY_KEY_INVALID');
    END IF;
    SELECT constraint_name INTO v_pk_name FROM user_constraints
     WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND constraint_type = 'P';
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY DROP CONSTRAINT ' || v_pk_name;
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY ADD CONSTRAINT team_migration_history_pk PRIMARY KEY (applied_sequence)';
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'TEAM_MIGRATION_HISTORY_ID_IX';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'CREATE INDEX team_migration_history_id_ix ON TEAM_MIGRATION_HISTORY (id, applied_sequence)';
  END IF;

  -- Existing v1 attempts are backfilled before action is constrained.
  SELECT COUNT(*) INTO v_count FROM user_tab_columns
   WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND column_name = 'ACTION';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_ATTEMPT ADD (action VARCHAR2(16))';
  END IF;
  UPDATE TEAM_MIGRATION_ATTEMPT SET action = 'migrate' WHERE action IS NULL;
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_ATTEMPT
   WHERE action IS NULL OR action NOT IN ('migrate', 'undo', 'redo');
  IF v_count <> 0 THEN
    RAISE_APPLICATION_ERROR(-20025, 'MIGRATION_ATTEMPT_ACTION_INVALID');
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_tab_columns
   WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND column_name = 'ACTION'
     AND data_type = 'VARCHAR2' AND data_length = 16 AND nullable = 'N';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_ATTEMPT MODIFY (action VARCHAR2(16) NOT NULL)';
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_constraints
   WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND constraint_name = 'TEAM_MIGRATION_ATTEMPT_ACTION_CK';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_ATTEMPT ADD CONSTRAINT team_migration_attempt_action_ck CHECK (action IN (''migrate'',''undo'',''redo''))';
  ELSE
    SELECT DBMS_LOB.SUBSTR(search_condition, 4000, 1) INTO v_condition
      FROM user_constraints
     WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND constraint_name = 'TEAM_MIGRATION_ATTEMPT_ACTION_CK';
    IF REGEXP_REPLACE(UPPER(v_condition), '[[:space:]]', '') <> 'ACTIONIN(''MIGRATE'',''UNDO'',''REDO'')' THEN
      RAISE_APPLICATION_ERROR(-20026, 'MIGRATION_ATTEMPT_ACTION_CONSTRAINT_INVALID');
    END IF;
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_tab_columns
   WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND column_name = 'CONFIRMATION_DIGEST';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_ATTEMPT ADD (confirmation_digest VARCHAR2(64))';
  ELSE
    SELECT COUNT(*) INTO v_count FROM user_tab_columns
     WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND column_name = 'CONFIRMATION_DIGEST'
       AND data_type = 'VARCHAR2' AND data_length = 64 AND nullable = 'Y';
    IF v_count = 0 THEN
      RAISE_APPLICATION_ERROR(-20027, 'MIGRATION_ATTEMPT_CONFIRMATION_COLUMN_INVALID');
    END IF;
  END IF;
  COMMIT;
END;
/

DECLARE
  v_count NUMBER;
  v_version NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_META
   WHERE project_id = __PROJECT_ID_LITERAL__;
  IF v_count > 1 THEN
    RAISE_APPLICATION_ERROR(-20028, 'MIGRATION_META_PROJECT_DUPLICATE');
  ELSIF v_count = 0 THEN
    INSERT INTO TEAM_MIGRATION_META (version_number, project_id, schema_set_digest)
    VALUES (2, __PROJECT_ID_LITERAL__, __SCHEMA_SET_DIGEST_LITERAL__);
  ELSE
    SELECT version_number INTO v_version FROM TEAM_MIGRATION_META
     WHERE project_id = __PROJECT_ID_LITERAL__;
    IF v_version NOT IN (1, 2) THEN
      RAISE_APPLICATION_ERROR(-20029, 'MIGRATION_META_VERSION_INVALID');
    END IF;
    IF v_version = 1 THEN
      UPDATE TEAM_MIGRATION_META SET version_number = 2,
          schema_set_digest = __SCHEMA_SET_DIGEST_LITERAL__
       WHERE project_id = __PROJECT_ID_LITERAL__ AND version_number = 1;
    END IF;
  END IF;
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_META
   WHERE project_id = __PROJECT_ID_LITERAL__ AND version_number = 2;
  IF v_count <> 1 THEN
    RAISE_APPLICATION_ERROR(-20030, 'MIGRATION_META_VERSION_NOT_TWO');
  END IF;
END;
/
MERGE INTO TEAM_MIGRATION_MUTEX d
USING (SELECT 1 singleton_id FROM dual) s
   ON (d.singleton_id = s.singleton_id)
WHEN NOT MATCHED THEN INSERT (singleton_id, owner_token, worker_identity, host)
VALUES (1, NULL, NULL, NULL);
COMMIT;
```

This text was generated (not hand-transcribed) by temporarily monkeypatching `teamlib.migration_store._sql_literal` in a throwaway script to emit the two placeholder tokens in place of the real quoted literals for a known `(project, schema_set_digest)` pair, then capturing the exact payload `SqlMigrationStore.bootstrap()` produces — guaranteeing the file is byte-identical to the runtime payload once the tokens are substituted back. Do not hand-edit either side without re-deriving the other the same way; `test_reference_sql_is_the_exact_runtime_bootstrap_payload` is the check that catches drift.

- [ ] **Step 4: Run the tests again to verify they pass**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_sql_metadata_store -v`
Expected: all tests PASS, including the byte-equality test. If it fails, diff `self.calls[-1][1]` against the rendered reference character-by-character (a trailing-newline or single-character DDL mismatch is the most likely cause) rather than loosening the assertion.

- [ ] **Step 5: Verify no CRLF line endings were introduced**

Run: `grep -RIl $'\r' scripts/sql/migration_metadata.sql; echo "exit=$?"`
Expected: `exit=1` (no match) — this mirrors the CI "JSON and line-ending checks" step, which fails the whole build on any CR byte in a `.sql` file.

- [ ] **Step 6: Run the full offline suite**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v`
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/sql/migration_metadata.sql scripts/tests/test_sql_metadata_store.py
git commit -m "$(cat <<'EOF'
fix: make migration_metadata.sql the exact runtime bootstrap payload

The checked-in reference file described the version-2 identity contract in
a comment but contained only flat CREATE TABLE DDL — it never established,
adopted, or refused-on-mismatch the project identity row the embedded
runtime bootstrap already enforces. The prior synchronization test only
checked a handful of comment/DDL tokens, so the two could (and did) drift
apart silently. The reference file is now the exact text
SqlMigrationStore.bootstrap() executes, with the two call-time-dynamic SQL
literals replaced by documented placeholder tokens; a new test proves
byte-for-byte equivalence instead of token matching, and covers the
previously-untested empty-version-1-adoption, non-empty-version-1-mismatch,
and project-identity-mismatch scenarios.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Full offline verification

**Files:** none (verification only)

- [ ] **Step 1: Run ruff**

Run: `python3 -m ruff check scripts/`
Expected: no findings. If `ruff` reports anything in a file this plan touched, fix it before proceeding; do not suppress with inline ignores unless an equivalent pattern already exists elsewhere in the file.

- [ ] **Step 2: Run the full Python test suite**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v`
Expected: every test passes, with the new tests from Tasks 1-6 present in the output.

- [ ] **Step 3: Run the repository's JSON/line-ending check for changed files**

Run:

```bash
find targets -name '*.json' -print0 | xargs -0 -n1 python3 -m json.tool >/dev/null
! grep -RIl $'\r' --include='*.apx' --include='*.sql' --include='*.json' --include='*.sh' --include='*.ps1' .
```

Expected: both succeed silently (the `grep` line is negated with `!`, so success means *no* match was found).

- [ ] **Step 4: Confirm the git diff only touches the files this plan named**

Run: `git status --short`
Expected: only `scripts/teamlib/qualification.py`, `scripts/tests/test_qualification.py`, `scripts/tests/test_online_workflows.py`, `scripts/teamlib/migrate.py`, `scripts/tests/test_migration_runner.py`, `scripts/team.py`, `scripts/teamlib/online_workflows.py`, `scripts/sql/migration_metadata.sql`, `scripts/tests/test_sql_metadata_store.py` appear as modified, plus this plan file and the design doc it implements. No `.env`, credential, or scratch/state directory should appear.

This task has no commit of its own — Tasks 1-6 already committed their own changes.

---

## Task 8: Protected acceptance (manual — not executable by this plan's agent)

**Files:** none (operator runbook)

Design section 4 requires running `run-integration` and `run-release-test` against prepared, non-production Oracle/APEX runners this development environment does not have access to. This task cannot be checked complete by an automated executor. Once Tasks 1-7 are merged, hand the following runbook to the target owner:

1. On the protected integration runner, run `run-integration` with one non-empty PASS migration verification and one declared application flow check. Confirm the resulting PASS evidence contains the observed toolchain digest and the latest accepted after-inventory digest.
2. On an isolated test target, run `run-release-test` with a disposable review migration whose verification returns `FAIL`. Confirm: exit code 3; no `APPLIED` event recorded for the failing migration; a `FAILED` attempt; a surfaced retained-recovery token in the CLI's recovery-context JSON line (Task 4's output); and no signable PASS evidence produced.
3. Recover only after the target owner reviews the observed state and supplies the required worker-termination evidence via `recover-migration --evidence <path> <run_token>` (the `run_token` comes directly from the JSON line Task 4 added).
4. Mark Task 10 step 5 of the original implementation plan (`docs/superpowers/plans/2026-09-10-repo-review-p1-remediation-and-flow-simplification.md`) complete only after both runs above have produced and retained their expected evidence — a queued, skipped, cancelled, or runner-unavailable workflow does not satisfy this gate.

- [ ] **Step 1: Hand off this runbook to the target owner and wait for their evidence before marking Task 10 step 5 of the original plan complete.**
