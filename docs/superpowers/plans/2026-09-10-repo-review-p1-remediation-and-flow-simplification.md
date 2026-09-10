# Repository Review P1 Remediation and Flow Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every priority-one repository-review finding and reduce the normal integration and release-test workflows to two safe, high-level commands without weakening explicit maintenance, recovery, signing, or production-owner boundaries.

**Architecture:** First repair the shared correctness primitives: assertion parsing, unknown-result classification, metadata identity, release-manifest verification, and promotion-evidence validation. Then add focused runtime-preflight and online-orchestration modules; the CLI and GitHub workflows become thin entry points while the existing low-level commands remain available for diagnosis and recovery.

**Tech Stack:** Python 3.10+ standard library, unittest, Oracle SQL/PLSQL through qualified SQLcl 26.2.1+, JDK 17+, APEX 26.1+, Oracle Database 23ai+, GitHub Actions, Bash, PowerShell, and `cryptography>=41` only for signing and signature verification.

**Spec:** `docs/superpowers/specs/2026-09-10-repo-review-p1-remediation-and-flow-simplification-design.md`

## Global Constraints

- Read the spec and repository `AGENTS.md` before implementation.
- Default push/pull-request CI remains offline and requires no Oracle, SQLcl, target credentials, or flow adapter.
- Integration and release-test writes use protected, prepared self-hosted runners and non-production targets only.
- Production writes remain refused in every executable path; production application remains an offline owner-operated runbook procedure.
- Preserve the isolated `METADATA` profile, read-only `VERIFY` profile, target identity, drift gate, mutex, attempts, observation chain, and evidence-driven recovery.
- Never generate rollback SQL or affirmative destructive confirmation.
- `migrate`, `undo-migration`, `redo-migration`, diagnostic commands, and recovery commands remain public low-level interfaces.
- Derive source commit and application aliases where the system already has authoritative values; do not ask workflows to repeat them.
- Test-first development is mandatory. Each task starts with a focused failing test and ends with its focused and neighboring suites passing.
- No command commits or pushes automatically. The commit commands below are explicit implementation checkpoints for the executing engineer.
- Do not claim fresh-install qualification. Uncaptured/transient Builder edits and arbitrary DML outside the supported inventory remain unobservable.

## File Structure

### New files

- `scripts/teamlib/assertions.py` — parse the shared framed SQL assertion protocol and run one migration verification member.
- `scripts/teamlib/runtime.py` — probe and validate the actual prepared-runner/toolchain/profile contract before online writes.
- `scripts/teamlib/online_workflows.py` — orchestrate the normal integration and release-test paths from existing safety primitives.
- `scripts/teamlib/evidence.py` — own the canonical version-2 evidence validator shared by signing and runbook generation.
- `scripts/sql/runtime_versions.sql` — emit framed database and APEX version observations through a read-only profile.
- `scripts/tests/test_assertions.py` — strict assertion protocol and migration verification tests.
- `scripts/tests/test_runtime.py` — prepared-runner and observed-toolchain preflight tests.
- `scripts/tests/test_online_workflows.py` — orchestration order, derivation, failure, and no-production tests.

### Existing files changed

- `scripts/team.py` — route migration verification through the shared helper and expose `run-integration` and `run-release-test`.
- `scripts/teamlib/qualification.py` — use shared assertion parsing, record the accepted after digest, and delegate evidence validation.
- `scripts/teamlib/runbook.py` — consume canonical protected-test evidence validation.
- `scripts/teamlib/sqlcl.py` — classify unknown results through wrapped exception cause chains.
- `scripts/teamlib/migrate.py` — preserve phase-aware UNKNOWN outcomes and avoid contradictory attempt-state writes.
- `scripts/teamlib/migration_store.py` — refuse schema-set rebinding before Oracle DDL and match JSON behavior.
- `scripts/sql/migration_metadata.sql` — keep the documented bootstrap contract synchronized with the embedded SQL.
- `scripts/teamlib/release.py` — derive and verify manifest metadata from archived payload bytes.
- `scripts/teamlib/release_adapter.py` — share strict migration verification and provide a live-history application primitive for orchestration.
- `.github/workflows/integration.yml` — use the prepared integration runner and one `run-integration` step.
- `.github/workflows/release.yml` — use the prepared test runner and one `run-release-test` step before signing and runbook generation.
- `ci/runner-contract.json` — declare runner labels/capabilities only if they are enforced by online preflight.
- `.env.example`, `README.md`, `docs/ci.md`, `docs/migrations.md`, `docs/promotion.md`, and `ci/app-checks/README.md` — document the final operator contract.
- Existing focused tests under `scripts/tests/` — update contract assertions without deleting safety coverage.

---

### Task 1: Enforce one framed SQL assertion protocol

**Files:**

- Create: `scripts/teamlib/assertions.py`
- Create: `scripts/tests/test_assertions.py`
- Modify: `scripts/teamlib/qualification.py:37-123`
- Modify: `scripts/team.py:313-335`
- Modify: `scripts/teamlib/release_adapter.py:140-178`
- Modify: `scripts/tests/test_app_checks.py`
- Modify: `scripts/tests/test_migration_runner.py`
- Modify: `scripts/tests/test_release_adapter.py`

**Interfaces:**

- Consumes: SQLcl result objects with a text `stdout` attribute.
- Produces: `parse_team_assertions(stdout: str) -> tuple[str, ...]` and `run_verification_member(profile, path, work, *, runner=run_sqlcl) -> tuple[str, ...]`.

- [ ] **Step 1: Write the parser failure and success tests**

```python
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from teamlib.assertions import (
    AssertionVerificationError,
    parse_team_assertions,
    run_verification_member,
)


class AssertionTests(unittest.TestCase):
    def test_all_unique_pass_rows_succeed(self):
        self.assertEqual(
            parse_team_assertions(
                "TEAM_ASSERT|table_exists|PASS\nTEAM_ASSERT|column_exists|PASS\n"
            ),
            ("table_exists", "column_exists"),
        )

    def test_fail_missing_malformed_and_duplicate_rows_refuse(self):
        cases = (
            ("TEAM_ASSERT|table_exists|FAIL\n", "failed assertions"),
            ("ordinary SQL output\n", "no TEAM_ASSERT rows"),
            ("TEAM_ASSERT|broken\n", "malformed TEAM_ASSERT row"),
            (
                "TEAM_ASSERT|same|PASS\nTEAM_ASSERT|same|PASS\n",
                "duplicate assertion",
            ),
        )
        for stdout, message in cases:
            with self.subTest(stdout=stdout):
                with self.assertRaisesRegex(AssertionVerificationError, message):
                    parse_team_assertions(stdout)

    def test_empty_member_is_deliberate_success_without_sqlcl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            member = root / "empty.verify.sql"
            member.write_bytes(b"")
            called = []
            result = run_verification_member(
                object(), member, root / "work",
                runner=lambda *args, **kwargs: called.append(args),
            )
        self.assertEqual(result, ())
        self.assertEqual(called, [])

    def test_nonempty_member_requires_framed_pass_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            member = root / "check.verify.sql"
            member.write_text(
                "SELECT 'TEAM_ASSERT|exists|FAIL' FROM dual;\n",
                encoding="utf-8",
            )
            runner = lambda *args, **kwargs: SimpleNamespace(
                stdout="TEAM_ASSERT|exists|FAIL\n"
            )
            with self.assertRaisesRegex(AssertionVerificationError, "exists"):
                run_verification_member(object(), member, root / "work", runner=runner)
```

- [ ] **Step 2: Run the new tests and confirm the missing module failure**

Run:

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_assertions -v
```

Expected: FAIL because `teamlib.assertions` does not exist.

- [ ] **Step 3: Implement strict parsing and the empty-member exception**

```python
"""Strict parsing for the shared TEAM_ASSERT SQL output protocol."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Callable

from .sqlcl import run_sqlcl


class AssertionVerificationError(RuntimeError):
    pass


_ASSERTION_RE = re.compile(r"^TEAM_ASSERT\|([^|]+)\|(PASS|FAIL)$")


def parse_team_assertions(stdout: str) -> tuple[str, ...]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith("TEAM_ASSERT|"):
            continue
        match = _ASSERTION_RE.fullmatch(line)
        if match is None:
            raise AssertionVerificationError(f"malformed TEAM_ASSERT row: {line}")
        name, status = match.groups()
        if name in seen:
            raise AssertionVerificationError(f"duplicate assertion: {name}")
        seen.add(name)
        rows.append((name, status))
    if not rows:
        raise AssertionVerificationError("verification returned no TEAM_ASSERT rows")
    failures = sorted(name for name, status in rows if status != "PASS")
    if failures:
        raise AssertionVerificationError(
            "failed assertions: " + ", ".join(failures)
        )
    return tuple(name for name, _ in rows)


def run_verification_member(
    profile: Any,
    path: str | Path,
    work: str | Path,
    *,
    runner: Callable[..., Any] = run_sqlcl,
) -> tuple[str, ...]:
    member = Path(path)
    if member.is_symlink() or not member.is_file():
        raise AssertionVerificationError(
            f"verification member is not a regular file: {member}"
        )
    if not member.read_bytes().strip():
        return ()
    result = runner(profile, "read", member, Path(work))
    return parse_team_assertions(getattr(result, "stdout", ""))
```

- [ ] **Step 4: Replace all three assertion implementations with the shared helper**

In `qualification._select_runner`, call `parse_team_assertions` and translate
`AssertionVerificationError` into the existing structured `FAIL` result:

```python
try:
    names = parse_team_assertions(getattr(result, "stdout", ""))
except AssertionVerificationError as exc:
    return {"status": "FAIL", "diagnostic": str(exc)}
return {"status": "PASS", "diagnostic": "", "assertions": list(names)}
```

In `team.py`, replace the unconditional `return True` with:

```python
def verify(migration, action, verify_path):
    run_verification_member(
        profile_target(config, "VERIFY"),
        verify_path,
        repo / "scratch" / "migration-verify" / action / migration.id,
    )
    return True
```

In `release_adapter.py`, use the same helper with the adapter's temporary work
directory:

```python
def verify(migration, action, verify_path):
    run_verification_member(
        profile_target(config, "VERIFY"),
        verify_path,
        work / "verify" / action / migration.id,
    )
    return True
```

Do not add a second parser.

- [ ] **Step 5: Add callback-level regressions**

Add one CLI callback test and one release-adapter callback test whose fake SQLcl
returns `TEAM_ASSERT|postcondition|FAIL`; assert that no history event is
recorded and the attempt is `FAILED`. Also retain explicit tests for forward,
undo, redo, and empty verification members.

- [ ] **Step 6: Run the focused suites**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_assertions \
  scripts.tests.test_app_checks \
  scripts.tests.test_migration_runner \
  scripts.tests.test_release_adapter -v
```

Expected: PASS.

- [ ] **Step 7: Commit the assertion boundary**

```bash
git add scripts/teamlib/assertions.py scripts/teamlib/qualification.py \
  scripts/team.py scripts/teamlib/release_adapter.py \
  scripts/tests/test_assertions.py scripts/tests/test_app_checks.py \
  scripts/tests/test_migration_runner.py scripts/tests/test_release_adapter.py
git commit -m "fix: enforce migration verification assertions"
```

---

### Task 2: Preserve wrapped and phase-specific UNKNOWN outcomes

**Files:**

- Modify: `scripts/teamlib/sqlcl.py:20-42,329-340`
- Modify: `scripts/teamlib/migrate.py:66-71,323-388`
- Modify: `scripts/tests/test_sqlcl.py`
- Modify: `scripts/tests/test_migration_runner.py`
- Modify: `docs/app-recovery.md`

**Interfaces:**

- Consumes: an exception and its `__cause__`/`__context__` chain.
- Produces: `result_is_unknown(exc: BaseException) -> bool` and phase-aware attempt handling in `_apply_operation`.

- [ ] **Step 1: Write exception-chain regressions**

```python
from teamlib.migration_store import MigrationStoreError
from teamlib.sqlcl import SqlclError, result_is_unknown


def test_wrapped_sqlcl_timeout_is_unknown(self):
    try:
        try:
            raise SqlclError("SQLcl timed out; target state is unknown")
        except SqlclError as exc:
            raise MigrationStoreError("metadata write failed") from exc
    except MigrationStoreError as wrapped:
        self.assertTrue(result_is_unknown(wrapped))


def test_deterministic_oracle_error_is_not_unknown(self):
    self.assertFalse(result_is_unknown(SqlclError("ORA-00942: table does not exist")))
```

Add a fake store that commits the event before losing acknowledgement:

```python
class CommitThenLoseEventAckStore(MigrationStore):
    def record_event(self, *args, **kwargs):
        super().record_event(*args, **kwargs)
        try:
            raise SqlclError("SQLcl timed out; target state is unknown")
        except SqlclError as exc:
            raise MigrationStoreError("metadata result unavailable") from exc
```

Assert `apply_plan` reports an unknown result, does not overwrite the committed
attempt with `FAILED`, and leaves the mutex for recovery.

- [ ] **Step 2: Run the focused tests and confirm failure**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_sqlcl \
  scripts.tests.test_migration_runner -v
```

Expected: FAIL because wrapped errors are not classified and the engine always
writes a second attempt state.

- [ ] **Step 3: Add cause-chain classification to `sqlcl.py`**

```python
_UNKNOWN_RESULT_MARKERS = (
    "timed out",
    "state is unknown",
    "acknowledg",
    "lost result",
)


def result_is_unknown(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SqlclError):
            text = str(current).casefold()
            if any(marker in text for marker in _UNKNOWN_RESULT_MARKERS):
                return True
        current = current.__cause__ or current.__context__
    return False
```

Delete the narrower duplicate `_result_is_unknown` from `migrate.py`.

- [ ] **Step 4: Track the operation phase and avoid contradictory writes**

Immediately before the inner operation block, initialize `phase = "execute"`.
Set it before each potentially state-changing boundary:

```python
phase = "execute"
_call_callback(execute, (migration, action, sql_path), (migration,))
phase = "verify"
verification = _call_callback(verify, (migration, action, verify_path), (migration,))
if verification is not True:
    raise MigrationRunError(f"verification failed for {migration_id}")
phase = "observe-after"
after_observation = _observation(observe, migration, "after")
after_digest = (
    after_observation.digest
    if after_observation
    else str(profiles.get("after_digest", ""))
)
evidence_digest = str(profiles.get("evidence_digest", ""))
if profiles.get("require_observation") and not after_digest:
    raise MigrationRunError(
        f"migration requires a verified after inventory: {migration_id}"
    )
if not evidence_digest and before_digest and after_digest:
    evidence_digest = hashlib.sha256(
        f"{before_digest}:{after_digest}".encode("ascii")
    ).hexdigest()
phase = "record-inventory"
if (
    after_observation
    and after_observation.manifest is not None
    and callable(record_inventory)
):
    record_inventory(
        target, after_observation.manifest, run_token=run_token
    )
phase = "record-event"
store.record_event(
    target,
    migration_id,
    migration.checksum,
    migration.target,
    migration.dependencies,
    str(profiles.get("source_commit", "unknown")),
    str(profiles.get("applied_by", worker)),
    {"before": before_digest, "after": after_digest, "evidence": evidence_digest},
    operation=operation,
    run_token=run_token,
    attempt_id=attempt_id,
)
phase = "complete"
```

Replace the generic exception branch with:

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
    raise MigrationRunError(
        f"migration payload failed during {phase}: {migration_id}: {exc}"
    ) from exc
```

Handle mutex release separately after the migration loop so an uncertain
release never rewrites the already-recorded attempt:

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
return _report(
    run_token, action, selected_ids, plan, applied, reverted,
    accepted_frontier, None,
)
```

- [ ] **Step 5: Cover each required phase**

Parameterize failures for `execute`, `verify`, after-inventory persistence,
`record_event`, `record_attempt_state`, and `release`. For each case assert the
attempt/history combination, retained or uncertain mutex, message, and absence
of a false `FAILED` claim after a possibly committed event.

- [ ] **Step 6: Run focused and recovery suites**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_sqlcl \
  scripts.tests.test_migration_runner \
  scripts.tests.test_migration_store \
  scripts.tests.test_recovery_flow -v
```

Expected: PASS.

- [ ] **Step 7: Commit unknown-result preservation**

```bash
git add scripts/teamlib/sqlcl.py scripts/teamlib/migrate.py \
  scripts/tests/test_sqlcl.py scripts/tests/test_migration_runner.py \
  docs/app-recovery.md
git commit -m "fix: preserve unknown migration outcomes"
```

---

### Task 3: Refuse metadata schema-set rebinding before Oracle DDL

**Files:**

- Modify: `scripts/teamlib/migration_store.py:71-361,1137-1291`
- Modify: `scripts/sql/migration_metadata.sql`
- Modify: `scripts/tests/test_sql_metadata_store.py`
- Modify: `scripts/tests/test_migration_store.py`

**Interfaces:**

- Consumes: the computed lowercase 64-character schema-set digest.
- Produces: `_validate_schema_set_digest(value: str) -> str` and matching SQL/JSON bootstrap identity behavior.

- [ ] **Step 1: Write bootstrap identity tests**

For the JSON store, cover fresh, repeated matching, empty legacy, and mismatched
identity:

```python
def test_bootstrap_refuses_schema_set_rebinding(self):
    first = "a" * 64
    self.store.bootstrap(self.target, schema_set_digest=first)
    self.store.bootstrap(self.target, schema_set_digest=first)
    with self.assertRaisesRegex(MigrationStoreError, "different project/schema set"):
        self.store.bootstrap(self.target, schema_set_digest="b" * 64)
```

For the SQL store, use a queued fake runner: the first read reports an existing
version-2 row with digest `"a" * 64`. Assert matching bootstrap reaches the
write call, while `"b" * 64` refuses after read-only preflight and emits no
write call.

- [ ] **Step 2: Run the metadata tests and confirm failure**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_sql_metadata_store \
  scripts.tests.test_migration_store -v
```

Expected: FAIL because SQL bootstrap overwrites the existing digest and digest
shape is not shared by both stores.

- [ ] **Step 3: Add one exact digest validator**

```python
def _validate_schema_set_digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise MigrationStoreError(
            "migration metadata bootstrap requires a lowercase schema-set SHA-256"
        )
    return value
```

Call it before either backend opens its write transaction.

- [ ] **Step 4: Preflight an existing Oracle metadata row before DDL**

Add a private SQL-store reader that first queries `user_tables`; only if the
table exists does it query the project row:

```python
def _existing_meta(self, store_target: Target) -> tuple[int, str] | None:
    exists = self._read_rows(
        store_target,
        "SELECT 'TEAM_META_TABLE|' || " + _b64_sql("TO_CHAR(COUNT(*))")
        + " FROM user_tables WHERE table_name = 'TEAM_MIGRATION_META';",
        "TEAM_META_TABLE|",
    )
    if exists == [["0"]]:
        return None
    if exists != [["1"]]:
        raise MigrationStoreError("migration metadata table probe is malformed")
    rows = self._read_rows(
        store_target,
        "SELECT 'TEAM_META_ID|' || "
        + _b64_sql("TO_CHAR(version_number)") + " || '|' || "
        + _b64_sql("schema_set_digest")
        + " FROM TEAM_MIGRATION_META WHERE project_id = "
        + _sql_literal(store_target.project) + ";",
        "TEAM_META_ID|",
    )
    if len(rows) != 1 or len(rows[0]) != 2:
        raise MigrationStoreError("migration metadata project identity is missing or duplicated")
    return int(rows[0][0]), rows[0][1]
```

Before appending `_MIGRATION_BOOTSTRAP_SQL`, enforce:

```python
existing = self._existing_meta(store_target)
if existing is not None:
    version, recorded = existing
    if version not in {1, 2}:
        raise MigrationStoreError("migration metadata version is unsupported")
    if recorded and recorded != schema_set_digest:
        raise MigrationStoreError("metadata store belongs to a different project/schema set")
```

The write SQL inserts on a fresh store, fills an empty version-1 digest, advances
version 1 to 2, and performs no version-2 digest update. Keep the reference SQL
semantically identical.

- [ ] **Step 5: Permit only explicit empty-legacy JSON adoption**

When a JSON v1 backup exists and its upgraded metadata digest is empty, allow
the first bootstrap to fill the supplied digest. Otherwise preserve exact
identity:

```python
meta = data.get("meta")
if isinstance(meta, dict) and meta.get("schema_set_digest") == "":
    if not self._backup_path.exists():
        raise MigrationStoreError("metadata store has an unowned schema set")
    meta = {**meta, "schema_set_digest": schema_set_digest}
identity = {
    "state_key": self._key(store_target),
    "project_id": store_target.project,
    "schema_set_digest": schema_set_digest,
}
if meta is not None and meta != identity:
    raise MigrationStoreError("metadata store belongs to a different project/schema set")
data["meta"] = identity
```

- [ ] **Step 6: Run metadata, migration, and production-boundary suites**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_sql_metadata_store \
  scripts.tests.test_migration_store \
  scripts.tests.test_migration_runner \
  scripts.tests.test_production_boundary -v
```

Expected: PASS.

- [ ] **Step 7: Commit metadata identity enforcement**

```bash
git add scripts/teamlib/migration_store.py scripts/sql/migration_metadata.sql \
  scripts/tests/test_sql_metadata_store.py scripts/tests/test_migration_store.py
git commit -m "fix: refuse migration metadata rebinding"
```

---

### Task 4: Bind release manifest metadata to payload bytes

**Files:**

- Modify: `scripts/teamlib/release.py:99-291,294-361,400-440`
- Modify: `scripts/tests/test_release.py`
- Modify: `scripts/tests/test_runbook.py`

**Interfaces:**

- Consumes: verified archive member bytes.
- Produces: `_migration_manifest(loaded: Mapping[str, Migration]) -> tuple[dict[str, Any], ...]` and a closed `_verify_archive_members` result.

- [ ] **Step 1: Add manifest/payload disagreement tests**

Add a helper to rebuild a test archive after changing only its manifest. Use it
to alter each migration field while retaining all generic payload hashes:

```python
def test_verify_rejects_migration_manifest_not_derived_from_payload(self):
    manifest = build_release(
        self.repo, self.commit, "1.2.3", Path(self.temp.name) / "out"
    )
    mutations = {
        "checksum": "0" * 64,
        "target": "code",
        "dependencies": [["foreign", "1" * 64]],
        "destructive": True,
        "reversible": True,
        "down_destructive": True,
    }
    for field, value in mutations.items():
        with self.subTest(field=field):
            tampered = self.rewrite_manifest(
                manifest.archive_path,
                lambda data: data["migrations"][0].__setitem__(field, value),
            )
            with self.assertRaisesRegex(ReleaseError, "migration metadata"):
                verify_release(tampered)
```

Add separate cases for a false `source_tree`, unknown/missing top-level field,
malformed source commit, and application-tree digest mismatch through direct
`verify_release` rather than only `release_app_trees`.

- [ ] **Step 2: Run release tests and confirm acceptance of tampered metadata**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_release -v
```

Expected: FAIL because the current verifier accepts manifest migration fields
that are not derived from archived SQL.

- [ ] **Step 3: Extract one canonical migration-manifest builder**

```python
def _migration_manifest(
    loaded: Mapping[str, Migration],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": item.id,
            "checksum": item.checksum,
            "target": item.target,
            "dependencies": [list(edge) for edge in item.dependencies],
            "destructive": item.destructive,
            "reversible": item.reversible,
            "down_destructive": item.down_destructive,
        }
        for item in sorted(loaded.values(), key=lambda value: (value.stamp, value.id))
    )
```

Use this helper in `build_release`; remove the duplicate inline mapping.

- [ ] **Step 4: Reconstruct archived migration bundles during verification**

Within a temporary directory, write only `release/migrations/<top-level-name>`
members, reject nested paths, call `load_bundles`, and compare exact canonical
values:

```python
with tempfile.TemporaryDirectory(prefix="team-release-verify-") as directory:
    migration_root = Path(directory)
    for path, member_bytes in members.items():
        if not path.startswith("release/migrations/"):
            continue
        relative = path.removeprefix("release/migrations/")
        if not relative or "/" in relative:
            raise ReleaseError(f"malformed packaged migration path: {path}")
        (migration_root / relative).write_bytes(member_bytes)
    derived = _migration_manifest(load_bundles(migration_root))
if tuple(data.get("migrations", ())) != derived:
    raise ReleaseError("release manifest migration metadata does not match payload")
```

Translate `BundleError` to `ReleaseError`.

- [ ] **Step 5: Close and recompute the complete manifest**

Require exactly these top-level keys:

```python
_MANIFEST_KEYS = {
    "format_version", "version", "source_commit", "source_tree", "toolchain",
    "migrations", "app_tree_digests", "master_contract_digest",
    "app_checks_digest", "payload_paths", "payload",
}
```

Validate version, semantic version, source commit, mappings/lists, and digest
shapes. Recompute:

```python
actual_source_tree = hashlib.sha256(_canonical(payload_records)).hexdigest()
if data["source_tree"] != actual_source_tree:
    raise ReleaseError("release manifest source tree does not match payload")
```

Build application groups from `members`, compare `tree_digest` for every alias,
and retain the existing master-contract/app-check digest checks. Only then call
`_manifest_from_data`.

- [ ] **Step 6: Make build self-verify its emitted archive**

After closing the tar and calculating its digest, call `verify_release(archive)`
and compare the returned digest with the built digest. Return the verified
manifest with `staging_dir` restored via `dataclasses.replace`.

- [ ] **Step 7: Run release and runbook suites**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_release \
  scripts.tests.test_release_adapter \
  scripts.tests.test_runbook -v
```

Expected: PASS.

- [ ] **Step 8: Commit archive verification**

```bash
git add scripts/teamlib/release.py scripts/tests/test_release.py \
  scripts/tests/test_runbook.py
git commit -m "fix: bind release metadata to payload bytes"
```

---

### Task 5: Canonicalize protected-test evidence validation

**Files:**

- Create: `scripts/teamlib/evidence.py`
- Modify: `scripts/teamlib/qualification.py:172-258,331-364,445-475`
- Modify: `scripts/teamlib/runbook.py:41-128,233-266`
- Modify: `scripts/tests/test_qualification.py`
- Modify: `scripts/tests/test_runbook.py`

**Interfaces:**

- Consumes: canonical version-2 evidence bytes.
- Produces: `validate_test_evidence(raw: bytes) -> dict[str, Any]`, shared by signing and runbook generation.

- [ ] **Step 1: Add role, environment, completeness, and frontier tests**

Extend the signing fixture to carry the complete identity emitted by
`qualification._target_identity`. Parameterize these rejected mutations:

```python
invalid_identities = (
    {**valid_identity, "role": "integration"},
    {**valid_identity, "environment": "staging"},
    {**valid_identity, "target_kind": "disposable"},
    {key: value for key, value in valid_identity.items() if key != "state_key"},
)
```

Assert both `sign_test_evidence` and `gen_runbook` reject each case. Update the
persistent report test so the latest observation contains different values:

```python
{"sequence": 3, "before": "b" * 64, "after": "c" * 64, "evidence": "d" * 64}
```

Then assert `observation_digest == "c" * 64`, not the transition evidence.
Also construct distinct TABLES and METADATA targets and assert
`_target_identity(...)["state_key"]` and `binding_digest` come from METADATA;
the signed evidence must identify the owner of migration history, not a payload
schema.

- [ ] **Step 2: Run the evidence suites and confirm failure**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_qualification \
  scripts.tests.test_runbook -v
```

Expected: FAIL because integration-role evidence is accepted and the report
uses `observation["evidence"]`.

- [ ] **Step 3: Implement one canonical evidence validator**

Move canonical JSON and version-2 PASS validation to `evidence.py`:

```python
TARGET_IDENTITY_KEYS = {
    "project", "role", "environment", "target_kind", "instance_id",
    "db_name", "service", "workspace_id", "app_ids", "state_key",
    "binding_digest",
}


def validate_test_evidence(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("test evidence is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise EvidenceError("test evidence is not canonical JSON")
    _validate_v2_shape(value)
    _validate_v2_digests(value)
    _validate_v2_results(value)
    identity = value.get("target_identity")
    if not isinstance(identity, dict) or set(identity) != TARGET_IDENTITY_KEYS:
        raise EvidenceError("test evidence target identity is incomplete")
    if identity["target_kind"] != "persistent":
        raise EvidenceError("test evidence target kind must be persistent")
    if identity["role"] != "test" or identity["environment"] != "test":
        raise EvidenceError("signed promotion evidence requires the protected test target")
    return value
```

Implement `_validate_v2_shape` with the exact current version-2 top-level key
set, `version == 2`, `final_status == "PASS"`, complete application-check
coverage with `unknown == 0`, and no unknown nested keys. Implement
`_validate_v2_digests` for archive, toolchain, observation, history,
`state_key`, and `binding_digest`. Implement `_validate_v2_results` to require
exactly the existing three PASS results. Validate all string identities as
non-empty, `workspace_id` as a positive integer, and `app_ids` as a non-empty
safe-alias-to-positive-integer mapping. Require `coverage.apps` to equal the
sorted application-ID aliases, require positive check coverage, and validate
each result against the existing closed `CheckResult.as_dict()` shape.

- [ ] **Step 4: Use the accepted after digest and share validation**

In `_base_report`:

```python
"observation_digest": _sha256_field(
    observation["after"], "observation digest"
),
```

In `_target_identity`, bind the evidence to the metadata owner:

```python
metadata = targets["METADATA"]
return {
    "project": config.project,
    "role": config.role,
    "environment": config.environment,
    "target_kind": "persistent",
    "instance_id": metadata.instance_id,
    "db_name": metadata.db_name,
    "service": metadata.service,
    "workspace_id": config.workspace_id,
    "app_ids": dict(sorted(config.apps.items())),
    "state_key": metadata.state_key,
    "binding_digest": metadata.binding_digest,
}
```

Have `sign_test_evidence` call `validate_test_evidence(raw)`. In `runbook.py`,
delete `_required_passes`; call the same validator and translate
`EvidenceError` into `RunbookError`. Signature verification still covers the
exact raw bytes.

- [ ] **Step 5: Run evidence, qualification, and runbook suites**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_qualification \
  scripts.tests.test_app_checks \
  scripts.tests.test_runbook -v
```

Expected: PASS.

- [ ] **Step 6: Commit the promotion boundary**

```bash
git add scripts/teamlib/evidence.py scripts/teamlib/qualification.py \
  scripts/teamlib/runbook.py scripts/tests/test_qualification.py \
  scripts/tests/test_runbook.py
git commit -m "fix: require protected test promotion evidence"
```

---

### Task 6: Add an observed prepared-runner preflight

**Files:**

- Create: `scripts/teamlib/runtime.py`
- Create: `scripts/sql/runtime_versions.sql`
- Create: `scripts/tests/test_runtime.py`
- Modify: `scripts/teamlib/qualification.py:272-322`
- Modify: `ci/runner-contract.json`
- Modify: `scripts/tests/test_ci_contract.py`

**Interfaces:**

- Consumes: validated `Config`, runner contract, flow executable, identity SQL, and version SQL.
- Produces: `RuntimeReport` and `preflight_online(config, repo, flow_executable, *, runner=run_sqlcl, command_runner=subprocess.run) -> RuntimeReport`.

- [ ] **Step 1: Write preflight tests before implementation**

```python
class RuntimePreflightTests(unittest.TestCase):
    def test_missing_sqlcl_and_flow_adapter_refuse_before_profile_probe(self):
        calls = []
        with self.assertRaisesRegex(RuntimeError, "SQLcl executable"):
            preflight_online(
                config_for(), self.root, "/missing/flow-runner",
                runner=lambda *args, **kwargs: calls.append(args),
                which=lambda name: None,
            )
        self.assertEqual(calls, [])

    def test_every_profile_identity_and_observed_version_is_bound(self):
        report = preflight_online(
            config_for(), self.root, str(self.flow_runner),
            runner=self.fake_sqlcl,
            which=self.fake_which,
            command_runner=self.fake_versions,
        )
        self.assertEqual(
            set(report.profiles),
            {"TABLES", "CODE", "APEX:employee", "METADATA", "VERIFY"},
        )
        self.assertRegex(report.toolchain_digest, r"^[0-9a-f]{64}$")
```

Also test an old SQLcl/JDK/APEX/database version, mismatched profile identity,
non-executable flow adapter, production config, and absent flow adapter when any
selected declaration contains a flow.

- [ ] **Step 2: Run the new tests and confirm the missing module failure**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_runtime -v
```

Expected: FAIL because `teamlib.runtime` does not exist.

- [ ] **Step 3: Add read-only framed database/APEX version SQL**

Create `scripts/sql/runtime_versions.sql` with only SELECT statements and strict
markers:

```sql
SET DEFINE OFF
SET HEADING OFF
SET FEEDBACK OFF
SET PAGESIZE 0
SELECT 'TEAM_RUNTIME|database|' || version_full
  FROM product_component_version
 WHERE product LIKE 'Oracle Database%';
SELECT 'TEAM_RUNTIME|apex|' || version_no
  FROM apex_release;
```

The Python parser requires exactly one value for each name and rejects malformed
or duplicate markers.

- [ ] **Step 4: Implement observed runtime validation**

```python
@dataclass(frozen=True)
class RuntimeReport:
    profiles: tuple[str, ...]
    versions: Mapping[str, str]
    capabilities: Mapping[str, str]
    target_state_keys: Mapping[str, str]
    toolchain_digest: str


def preflight_online(
    config: Config,
    repo: str | Path,
    flow_executable: str,
    *,
    runner: Callable[..., Any] = run_sqlcl,
    which: Callable[[str], str | None] = shutil.which,
    command_runner: Callable[..., Any] = subprocess.run,
) -> RuntimeReport:
    if config.environment == "production":
        raise RuntimeError("online qualification preflight refuses production")
    repo_path = Path(repo)
    sqlcl = which(os.environ.get("SQLCL_BIN", "sql"))
    java = which("java")
    if not sqlcl:
        raise RuntimeError("SQLcl executable is unavailable")
    if not java:
        raise RuntimeError("JDK executable is unavailable")
    if config.apps:
        flow = Path(flow_executable)
        if flow.is_symlink() or not flow.is_file() or not os.access(flow, os.X_OK):
            raise RuntimeError("TEAM_FLOW_RUNNER is not an executable regular file")
    sqlcl_version = _command_version([sqlcl, "-version"], command_runner)
    jdk_version = _command_version([java, "-version"], command_runner)
    targets = {
        name: profile_target(config, name)
        for name in ("TABLES", "CODE", "METADATA", "VERIFY")
    }
    for alias in sorted(config.apps):
        targets[f"APEX:{alias}"] = profile_target(config, "APEX", alias=alias)
    identity_driver = repo_path / "scripts" / "sql" / "identity.sql"
    for name, target in targets.items():
        runner(target, "read", identity_driver, repo_path / "scratch" / "preflight" / "identity" / name)
    versions_result = runner(
        targets["VERIFY"],
        "read",
        repo_path / "scripts" / "sql" / "runtime_versions.sql",
        repo_path / "scratch" / "preflight" / "versions",
    )
    remote_versions = _parse_runtime_versions(getattr(versions_result, "stdout", ""))
    cryptography_version = _probe_ed25519()
    versions = {
        "python": platform.python_version(),
        "sqlcl": sqlcl_version,
        "jdk": jdk_version,
        "apex": remote_versions["apex"],
        "database": remote_versions["database"],
        "cryptography": cryptography_version,
    }
    capabilities = {"cryptography": "Ed25519-qualified"}
    contract_path = repo_path / "ci" / "runner-contract.json"
    contract_version = _require_contract(contract_path, versions, capabilities)
    target_state_keys = {name: target.state_key for name, target in targets.items()}
    digest = hashlib.sha256(
        json.dumps(
            {
                "contract_version": contract_version,
                "versions": versions,
                "capabilities": capabilities,
                "targets": target_state_keys,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return RuntimeReport(
        tuple(sorted(targets)), versions, capabilities, target_state_keys, digest
    )
```

Implement `_command_version` with `check=False`, captured text, and nonzero/empty
output refusal. Implement `_parse_runtime_versions` with a strict
`TEAM_RUNTIME|name|value` regular expression and exact `{database, apex}` key
set. Every valid application declaration already requires a flow check, so an
application-bearing config requires the executable adapter before any profile
probe. Implement `_probe_ed25519` by importing `cryptography`, generating an
`Ed25519PrivateKey`, signing fixed bytes, and verifying the signature with its
public key; refuse import, signing, or verification failure.

Implement `_require_contract` with this closed requirement grammar:

- a requirement of the form `<numeric>+`, where `<numeric>` is one or more
  dot-separated integers, compares the first observed dotted numeric tuple with the
  required tuple, padding the shorter tuple with zeros;
- an optional `ai` suffix is accepted only in the requirement, so `23ai+`
  means an Oracle database major version of at least 23; and
- `Ed25519-qualified` is satisfied only by the successful `_probe_ed25519`
  capability, never by the installed package version alone.

Reject unknown toolchain keys, missing keys, unparsable required values, and
observed output with no numeric version instead of guessing. Return the
contract's integer `version` so the digest binds the observed runtime to the
contract version.

Compute `toolchain_digest` from canonical observed versions plus the contract
version, not merely from declared requirement strings.

- [ ] **Step 5: Thread the observed digest into qualification**

Add a keyword-only `runtime_report: RuntimeReport` parameter to
`qualify_target`. Require it for online callers and use
`runtime_report.toolchain_digest`. Direct tests supply a valid fake report;
production code obtains it before any metadata write.

- [ ] **Step 6: Run runtime, config, CI, and qualification suites**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_runtime \
  scripts.tests.test_config \
  scripts.tests.test_ci_contract \
  scripts.tests.test_qualification -v
```

Expected: PASS.

- [ ] **Step 7: Commit observed runtime preflight**

```bash
git add scripts/teamlib/runtime.py scripts/sql/runtime_versions.sql \
  scripts/teamlib/qualification.py ci/runner-contract.json \
  scripts/tests/test_runtime.py scripts/tests/test_ci_contract.py \
  scripts/tests/test_qualification.py
git commit -m "feat: verify online runner capabilities"
```

---

### Task 7: Add the single-command integration path

**Files:**

- Create: `scripts/teamlib/online_workflows.py`
- Create: `scripts/tests/test_online_workflows.py`
- Modify: `scripts/team.py:47-144,313-470`
- Modify: `scripts/tests/test_integration_workflow.py`
- Modify: `scripts/tests/test_production_boundary.py`

**Interfaces:**

- Consumes: repository path, validated integration config, output path, protected flow-adapter path, and existing stores/runners.
- Produces: `run_integration(repo: Path, config: Config, out: Path, *, flow_executable: str, dependencies: OnlineDependencies | None = None) -> OnlineRunResult`.

- [ ] **Step 1: Write the orchestration-order and derivation tests**

Use a dependency recorder rather than SQLcl:

```python
def test_run_integration_derives_head_and_aliases_and_orders_gates(self):
    events = []
    dependencies = fake_dependencies(events=events, head="a" * 40)
    result = run_integration(
        self.repo,
        config_for(apps={"employee": 101}),
        self.root / "qualification.json",
        flow_executable=str(self.flow_runner),
        dependencies=dependencies,
    )
    self.assertEqual(result.source_commit, "a" * 40)
    self.assertEqual(dependencies.qualified_aliases, ("employee",))
    self.assertEqual(
        events,
        [
            "preflight", "setup-control", "bootstrap-metadata",
            "capture-before", "read-frontier", "check-drift", "migrate",
            "deploy:employee", "qualify", "write-report",
        ],
    )
```

Also test:

- production refuses before `preflight`;
- existing frontier skips adoption;
- absent frontier with history refuses;
- fresh empty metadata adopts sequence zero;
- drift refuses before migration/deployment;
- destructive pending work returns `maintenance-required` plus the exact
  confirmation template and performs no payload;
- failed/unknown migration or deployment stops later steps; and
- report output is canonical and preserves structured failure evidence.

- [ ] **Step 2: Run the new tests and confirm the missing module failure**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_online_workflows -v
```

Expected: FAIL because `teamlib.online_workflows` does not exist.

- [ ] **Step 3: Define injectable dependencies and the result contract**

```python
@dataclass(frozen=True)
class OnlineDependencies:
    resolve_head: Callable[[Path], str]
    preflight: Callable[..., RuntimeReport]
    setup_control: Callable[[Path, Config], None]
    bootstrap_metadata: Callable[[Path, Config], Any]
    read_state: Callable[[Any, Target], Mapping[str, Any]]
    adopt_frontier: Callable[[Path, Config, Any, Target, Any], str]
    capture_inventory: Callable[[Path, Config, str], Any]
    check_drift: Callable[[Path, Any], None]
    apply_migrations: Callable[[Path, Config, Any, str], Any]
    deploy_apps: Callable[[Path, Config, str], tuple[Any, ...]]
    qualify_integration: Callable[..., Mapping[str, Any]]
    verify_release: Callable[[Path], Manifest]
    apply_release_live: Callable[..., ApplyReport]
    qualify_release: Callable[..., Mapping[str, Any]]
    write_report: Callable[[Mapping[str, Any], Path], None]


@dataclass(frozen=True)
class OnlineRunResult:
    status: str
    source_commit: str
    report: Mapping[str, Any] | None
    confirmation_template: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_commit": self.source_commit,
            "report": dict(self.report) if self.report is not None else None,
            "confirmation_template": (
                dict(self.confirmation_template)
                if self.confirmation_template is not None
                else None
            ),
        }
```

Provide real defaults in one `_default_dependencies()` function; tests inject
fakes. Do not pass raw globals through every call site.

- [ ] **Step 4: Implement the fail-closed integration sequence**

The implementation must make the order visible and linear:

```python
def run_integration(repo, config, out, *, flow_executable, dependencies=None):
    if config.role != "integration" or config.environment == "production":
        raise OnlineWorkflowError("run-integration requires a non-production integration target")
    deps = dependencies or _default_dependencies()
    repo_path = Path(repo)
    source_commit = deps.resolve_head(repo_path)
    aliases = tuple(sorted(config.apps))
    runtime = deps.preflight(config, repo_path, flow_executable)
    deps.setup_control(repo_path, config)
    store = deps.bootstrap_metadata(repo_path, config)
    metadata = profile_target(config, "METADATA")
    before = deps.capture_inventory(repo_path, config, "integration-before")
    state = deps.read_state(store, metadata)
    observations = state.get("observations", [])
    history = store.read_history(metadata)
    if not observations:
        if history:
            raise OnlineWorkflowError(
                "migration history exists without an observed frontier"
            )
        deps.adopt_frontier(repo_path, config, store, metadata, before)
    deps.check_drift(repo_path / "database" / "schema-inventory.json", before)
    migration_report = deps.apply_migrations(
        repo_path, config, store, before.digest
    )
    if migration_report.confirmation_template is not None:
        return OnlineRunResult(
            "maintenance-required",
            source_commit,
            None,
            migration_report.confirmation_template,
        )
    deps.deploy_apps(repo_path, config, source_commit)
    report = deps.qualify_integration(
        repo_path,
        config,
        source_commit,
        aliases,
        store=store,
        flow_executable=flow_executable,
        runtime_report=runtime,
    )
    deps.write_report(report, Path(out))
    return OnlineRunResult("PASS", source_commit, report)
```

The real `adopt_frontier` dependency receives `before`, acquires the mutex,
records that exact inventory without recapturing it, calls `ensure_observation`,
and releases only after success. This preserves the one-capture gate even on a
fresh metadata owner.

The real `apply_migrations` dependency first calls `apply_plan` with
`dry_run=True`, the same store, targets, mode, and local bundles. If the dry-run
report contains a confirmation template, return it immediately without an
online apply call. Otherwise call `apply_plan` live with the strict
execute/verify/observe callbacks, `verified_inventory_digest=before.digest`,
and `require_observation=True`. The live call still reacquires the mutex, reads
history, and recomputes pending work before its first payload. Assert in tests
that the destructive preview performs no payload and that a race which changes
the live plan is refused by the live recomputation.

- [ ] **Step 5: Add the CLI without repeated commit or alias arguments**

Parser:

```python
integration = sub.add_parser("run-integration", parents=[env_parent])
integration.add_argument("--out", required=True)
```

Dispatch:

```python
if command == "run-integration":
    config = _config(args, require_verify=True)
    result = run_integration(
        repo,
        config,
        Path(args.out),
        flow_executable=os.environ.get("TEAM_FLOW_RUNNER", ""),
    )
    _json(result.as_dict())
    return 0 if result.status == "PASS" else 3
```

Add `run-integration` to `PRODUCTION_REFUSED_COMMANDS`. Keep `qualify-target`,
`setup-state`, `adopt-frontier`, `check-drift`, `migrate`, and `deploy-app` for
diagnosis/maintenance; do not remove them.

- [ ] **Step 6: Run orchestration, CLI, and production-boundary suites**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_online_workflows \
  scripts.tests.test_integration_workflow \
  scripts.tests.test_production_boundary \
  scripts.tests.test_migration_runner \
  scripts.tests.test_deploy -v
```

Expected: PASS.

- [ ] **Step 7: Commit the integration orchestrator**

```bash
git add scripts/teamlib/online_workflows.py scripts/team.py \
  scripts/tests/test_online_workflows.py \
  scripts/tests/test_integration_workflow.py \
  scripts/tests/test_production_boundary.py
git commit -m "feat: add single-command integration run"
```

---

### Task 8: Add the live-history release-test path

**Files:**

- Modify: `scripts/teamlib/online_workflows.py`
- Modify: `scripts/teamlib/release_adapter.py:69-200`
- Modify: `scripts/teamlib/qualification.py:202-408`
- Modify: `scripts/team.py:47-144,665-704`
- Modify: `scripts/tests/test_online_workflows.py`
- Modify: `scripts/tests/test_release_adapter.py`
- Modify: `scripts/tests/test_qualification.py`

**Interfaces:**

- Consumes: verified release archive, test target contract, validated test config, protected flow adapter, and output path.
- Produces: `run_release_test(repo: Path, config: Config, release_tar: Path, target_contract: Path, out: Path, *, flow_executable: str, dependencies: OnlineDependencies | None = None) -> OnlineRunResult`.

- [ ] **Step 1: Write live-history and internal-handoff tests**

```python
def test_release_test_reads_live_history_and_emits_evidence_without_plan_files(self):
    events = []
    dependencies = fake_dependencies(events=events, head="a" * 40)
    dependencies.live_history = {"m1": {"status": "APPLIED", "checksum": "b" * 64}}
    result = run_release_test(
        self.repo,
        config_for(role="test", environment="test", apps={"employee": 201}),
        self.release_tar,
        self.repo / "targets" / "test.json",
        self.root / "test-evidence.json",
        flow_executable=str(self.flow_runner),
        dependencies=dependencies,
    )
    self.assertEqual(result.status, "PASS")
    self.assertIn("read-live-history", events)
    self.assertNotIn("read-history-file", events)
    self.assertFalse((self.root / "plan.json").exists())
    self.assertFalse((self.root / "apply-report.json").exists())
```

Also assert archive source commit and aliases are derived, role must be exactly
`test`, target/config bindings match, planning is recomputed under the migration
mutex, destructive release work refuses, and apply/qualification failure emits
no PASS evidence.

- [ ] **Step 2: Run focused tests and confirm failure**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_online_workflows \
  scripts.tests.test_release_adapter -v
```

Expected: FAIL because the release adapter still requires caller-supplied plan
and history files and no high-level release-test function exists.

- [ ] **Step 3: Refactor release application around live store history**

Add a Python API that receives already validated objects, not plan/history JSON
paths:

```python
@dataclass(frozen=True)
class ReleaseApplyContext:
    manifest: Manifest
    target_document: Mapping[str, Any]
    config: Config
    metadata: Target
    migration_store: SqlMigrationStore
    control_store: SqlControlStore
    schema_set_digest: str
    app_targets: tuple[Target, ...]


def apply_verified_release_live(
    release_tar: str | Path,
    target_contract: str | Path,
    config: Config,
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
) -> ApplyReport:
    context = _validated_release_context(
        release_tar,
        target_contract,
        config,
        repo=Path(repo),
        root=Path(root) if root is not None else None,
    )
    context.migration_store.bootstrap(
        context.metadata,
        schema_set_digest=context.schema_set_digest,
    )
    history = context.migration_store.read_history(context.metadata)
    plan = plan_release(release_tar, history, context.target_document)
    pending_by_id = {
        item["id"]: item for item in context.manifest.migrations
        if item["id"] in plan.pending
    }
    destructive = tuple(
        migration_id for migration_id in plan.pending
        if pending_by_id[migration_id]["destructive"]
    )
    if destructive:
        raise ReleaseAdapterError(
            "release-test requires reviewed destructive maintenance before apply: "
            + ", ".join(destructive)
        )
    context.control_store.setup_state(context.app_targets)
    return _apply_release_context(context, release_tar, plan, history)
```

`_validated_release_context` performs, without writes: archive verification;
test-role/non-production checks; config/target project, role, environment,
instance, database, service, workspace, app-ID, and profile binding equality;
schema-set digest calculation; app-tree extraction; and construction of all
targets and stores. `_apply_release_context` contains the existing temporary
migration extraction, strict execute/verify/observe callbacks, `apply_plan`
expected-pending comparison, application deployment, and `apply_release` call.
The destructive check occurs after live history planning but before application
controller setup or payload execution. `run-release-test` never accepts or
constructs a destructive-confirmation document; the separate reviewed
maintenance path must make the target current before the release-test rerun.

The existing file-oriented `apply-release` CLI may remain for offline/manual
compatibility, but it must delegate to safe shared internals and may not be used
by `run-release-test` or the release workflow.

- [ ] **Step 4: Accept an in-memory apply report during qualification**

Replace the path-only `_check_apply_report` with:

```python
def _check_apply_report(
    value: Mapping[str, Any] | str | Path,
    *,
    source_commit: str,
    archive_digest: str,
    target_state_key: str,
) -> None:
    document = _load_json(value, "apply report")[1] if isinstance(value, (str, Path)) else dict(value)
    allowed = {
        "version", "status", "source_commit", "archive_digest",
        "target_state_key", "target_digest", "history_digest", "pending",
    }
    if set(document) != allowed:
        raise QualificationError("apply report has an unexpected shape")
    if document["version"] != 1 or document["status"] != "applied":
        raise QualificationError("apply report is not a successful version-1 report")
    if document["source_commit"] != source_commit:
        raise QualificationError("apply report source commit does not match qualification")
    if document["archive_digest"] != archive_digest:
        raise QualificationError("apply report archive does not match qualification")
    if document["target_state_key"] != target_state_key:
        raise QualificationError("apply report target identity does not match qualification target")
    _sha256_field(document["target_digest"], "apply report target_digest")
    _sha256_field(document["history_digest"], "apply report history_digest")
    if not isinstance(document["pending"], list) or any(
        not isinstance(item, str) or not item for item in document["pending"]
    ):
        raise QualificationError("apply report pending must be a list of migration IDs")
```

This removes the workflow-level apply-report file without weakening its
identity validation.

- [ ] **Step 5: Implement the release-test orchestration**

```python
def run_release_test(
    repo: str | Path,
    config: Config,
    release_tar: str | Path,
    target_contract: str | Path,
    out: str | Path,
    *,
    flow_executable: str,
    dependencies: OnlineDependencies | None = None,
) -> OnlineRunResult:
    if config.role != "test" or config.environment != "test":
        raise OnlineWorkflowError("run-release-test requires the protected test target")
    deps = dependencies or _default_dependencies()
    repo_path = Path(repo)
    archive_path = Path(release_tar)
    target_path = Path(target_contract)
    manifest = deps.verify_release(archive_path)
    archive_aliases = tuple(sorted(manifest.app_tree_digests))
    config_aliases = tuple(sorted(config.apps))
    if archive_aliases != config_aliases:
        raise OnlineWorkflowError(
            "release archive and test configuration application bindings differ"
        )
    runtime = deps.preflight(config, repo_path, flow_executable)
    apply_report = deps.apply_release_live(
        archive_path, target_path, config, repo=repo_path
    )
    report = deps.qualify_release(
        repo_path,
        config,
        manifest.source_commit,
        config_aliases,
        release_archive=archive_path,
        apply_report=apply_report.as_dict(),
        flow_executable=flow_executable,
        runtime_report=runtime,
    )
    deps.write_report(report, Path(out))
    return OnlineRunResult("PASS", manifest.source_commit, report)
```

`apply_verified_release_live` performs the target-contract alias comparison as
part of `_validated_release_context`, so all three alias sets are equal before
its first metadata write.

- [ ] **Step 6: Add the CLI route**

```python
release_test = sub.add_parser("run-release-test", parents=[env_parent])
release_test.add_argument("archive")
release_test.add_argument("--target", required=True)
release_test.add_argument("--out", required=True)
```

Load the environment with `require_verify=True`, obtain `TEAM_FLOW_RUNNER`, call
`run_release_test`, print its compact status, and add the command to
`PRODUCTION_REFUSED_COMMANDS`. It is an online command; do not route it through
`OFFLINE_COMMANDS`.

- [ ] **Step 7: Run release-test, qualification, and production suites**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_online_workflows \
  scripts.tests.test_release_adapter \
  scripts.tests.test_qualification \
  scripts.tests.test_release \
  scripts.tests.test_production_boundary -v
```

Expected: PASS.

- [ ] **Step 8: Commit the release-test orchestrator**

```bash
git add scripts/teamlib/online_workflows.py scripts/teamlib/release_adapter.py \
  scripts/teamlib/qualification.py scripts/team.py \
  scripts/tests/test_online_workflows.py \
  scripts/tests/test_release_adapter.py scripts/tests/test_qualification.py
git commit -m "feat: add live-history release test run"
```

---

### Task 9: Convert online workflows to the slimmer prepared-runner flow

**Files:**

- Modify: `.github/workflows/integration.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `.env.example`
- Modify: `scripts/tests/test_integration_workflow.py`
- Modify: `scripts/tests/test_release_workflow.py`
- Modify: `scripts/tests/test_docs.py`

**Interfaces:**

- Consumes: protected self-hosted runner labels, environment-profile secret content, executable flow-adapter path, signing key content, public trust key, and production history.
- Produces: one online command per integration/test workflow and unchanged signed runbook artifacts.

- [ ] **Step 1: Rewrite workflow tests before YAML**

Integration assertions:

```python
self.assertIn("runs-on: [self-hosted, team-apex, integration]", workflow)
self.assertEqual(workflow.count("run-integration"), 1)
for removed in ("setup-state", "adopt-frontier", "check-drift", "migrate ", "deploy-app", "qualify-target"):
    self.assertNotIn(removed, workflow)
self.assertIn("TEAM_FLOW_RUNNER: ${{ vars.TEAM_FLOW_RUNNER }}", workflow)
self.assertIn("TEAM_ENV_CONTENT: ${{ secrets.TEAM_ENV_CONTENT }}", workflow)
```

Release assertions:

```python
self.assertIn("runs-on: [self-hosted, team-apex, test]", release)
self.assertEqual(release.count("run-release-test"), 1)
self.assertNotIn("TEAM_TEST_HISTORY_JSON", release)
self.assertNotIn("apply_release.sh", release)
self.assertNotIn("test-plan.json", release)
self.assertNotIn("apply-report.json", release)
self.assertIn("sign-test-evidence", release)
self.assertIn("gen-runbook", release)
self.assertIn("TEAM_FLOW_RUNNER: ${{ vars.TEAM_FLOW_RUNNER }}", release)
```

Retain assertions for exact checkout, serial integration, protected
environments, signing-key permissions/cleanup, diagnostics upload, no production
credentials, and no production apply.

- [ ] **Step 2: Run workflow tests and confirm failure**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_integration_workflow \
  scripts.tests.test_release_workflow -v
```

Expected: FAIL because both workflows still use `ubuntu-latest` and the old
multi-step handoffs.

- [ ] **Step 3: Reduce `integration.yml` to materialization plus one command**

Use:

```yaml
jobs:
  qualification:
    runs-on: [self-hosted, team-apex, integration]
    environment: integration
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.sha }}
      - name: Materialize protected integration profile
        env:
          TEAM_ENV_CONTENT: ${{ secrets.TEAM_ENV_CONTENT }}
        run: |
          test -n "$TEAM_ENV_CONTENT"
          umask 077
          printf '%s\n' "$TEAM_ENV_CONTENT" > "$RUNNER_TEMP/integration.env"
      - name: Run integration gate
        env:
          TEAM_FLOW_RUNNER: ${{ vars.TEAM_FLOW_RUNNER }}
        run: |
          PYTHONPATH=scripts python3 scripts/team.py \
            --env "$RUNNER_TEMP/integration.env" run-integration \
            --out "$RUNNER_TEMP/qualification.json"
      - name: Remove protected integration profile
        if: always()
        run: find "$RUNNER_TEMP" -maxdepth 1 -name integration.env -type f -delete
```

Keep the existing `if: always()` report upload, but use
`if-no-files-found: warn` because preflight can correctly refuse before a
report exists. Add a cleanup test that the exact bounded profile path is
removed.

- [ ] **Step 4: Reduce the release test job to one online command**

Keep build/upload and post-download verification. Change only the online test
job:

```yaml
  test-and-handoff:
    needs: build
    runs-on: [self-hosted, team-apex, test]
    environment: test
```

Materialize `TEAM_TEST_ENV_CONTENT` under `RUNNER_TEMP`, explicitly map
`TEAM_FLOW_RUNNER`, and run:

```yaml
      - name: Apply and qualify the release on protected test
        env:
          TEAM_FLOW_RUNNER: ${{ vars.TEAM_FLOW_RUNNER }}
        run: |
          PYTHONPATH=scripts python3 scripts/team.py \
            --env "$RUNNER_TEMP/test.env" run-release-test \
            scratch/release/release.tar --target targets/test.json \
            --out "$RUNNER_TEMP/test-evidence.json"
```

Retain signing and runbook generation as separate steps. Replace external file
paths with protected content and materialize every handoff input beneath
`RUNNER_TEMP`:

```yaml
      - name: Materialize protected handoff inputs
        env:
          TEAM_TEST_ENV_CONTENT: ${{ secrets.TEAM_TEST_ENV_CONTENT }}
          TEAM_TEST_SIGNING_KEY_CONTENT: ${{ secrets.TEAM_TEST_SIGNING_KEY_CONTENT }}
          TEAM_TRUST_KEY_CONTENT: ${{ vars.TEAM_TRUST_KEY_CONTENT }}
          TEAM_PRODUCTION_HISTORY_CONTENT: ${{ vars.TEAM_PRODUCTION_HISTORY_CONTENT }}
        run: |
          test -n "$TEAM_TEST_ENV_CONTENT"
          test -n "$TEAM_TEST_SIGNING_KEY_CONTENT"
          test -n "$TEAM_TRUST_KEY_CONTENT"
          test -n "$TEAM_PRODUCTION_HISTORY_CONTENT"
          umask 077
          printf '%s\n' "$TEAM_TEST_ENV_CONTENT" > "$RUNNER_TEMP/test.env"
          printf '%s\n' "$TEAM_TEST_SIGNING_KEY_CONTENT" > "$RUNNER_TEMP/test-signing-key.pem"
          printf '%s\n' "$TEAM_TRUST_KEY_CONTENT" > "$RUNNER_TEMP/test-trust-key.pem"
          printf '%s\n' "$TEAM_PRODUCTION_HISTORY_CONTENT" > "$RUNNER_TEMP/production-history.json"
          chmod 600 "$RUNNER_TEMP/test.env" "$RUNNER_TEMP/test-signing-key.pem" \
            "$RUNNER_TEMP/test-trust-key.pem" "$RUNNER_TEMP/production-history.json"
```

Use those exact paths in the separate signing and runbook commands:

```yaml
      - name: Sign test evidence
        run: |
          PYTHONPATH=scripts python3 scripts/team.py sign-test-evidence \
            --evidence "$RUNNER_TEMP/test-evidence.json" \
            --private-key "$RUNNER_TEMP/test-signing-key.pem" \
            --out "$RUNNER_TEMP/test-evidence.sig"
      - name: Generate offline production handoff
        run: |
          PYTHONPATH=scripts python3 scripts/team.py gen-runbook \
            scratch/release/release.tar \
            --history "$RUNNER_TEMP/production-history.json" \
            --target targets/production.json \
            --test-evidence "$RUNNER_TEMP/test-evidence.json" \
            --signature "$RUNNER_TEMP/test-evidence.sig" \
            --trust-key "$RUNNER_TEMP/test-trust-key.pem" \
            --out scratch/PRODUCTION_RUNBOOK.md
      - name: Remove protected handoff inputs
        if: always()
        run: |
          find "$RUNNER_TEMP" -maxdepth 1 -type f \
            \( -name test.env -o -name test-signing-key.pem \
               -o -name test-trust-key.pem -o -name production-history.json \) \
            -delete
```

Upload test evidence and its detached signature in the diagnostic artifact;
remove the obsolete apply-report path. Never upload any materialized key,
environment profile, or production-history file.

- [ ] **Step 5: Run workflow, launcher, and docs tests**

```bash
PYTHONPATH=scripts python3 -m unittest \
  scripts.tests.test_integration_workflow \
  scripts.tests.test_release_workflow \
  scripts.tests.test_launchers \
  scripts.tests.test_docs -v
```

Expected: PASS.

- [ ] **Step 6: Commit the workflow conversion**

```bash
git add .github/workflows/integration.yml .github/workflows/release.yml \
  .env.example scripts/tests/test_integration_workflow.py \
  scripts/tests/test_release_workflow.py scripts/tests/test_docs.py
git commit -m "ci: simplify protected online workflows"
```

---

### Task 10: Align documentation and prove the complete repository

**Files:**

- Modify: `README.md`
- Modify: `docs/ci.md`
- Modify: `docs/migrations.md`
- Modify: `docs/promotion.md`
- Modify: `ci/app-checks/README.md`
- Modify: `docs/superpowers/specs/2026-09-10-staging-qualification-and-undo-design.md`
- Modify: `scripts/tests/test_docs.py`

**Interfaces:**

- Consumes: final CLI and workflow behavior from Tasks 1–9.
- Produces: one consistent daily, integration, release-test, maintenance, recovery, and production-owner guide.

- [ ] **Step 1: Add documentation contract assertions**

```python
required = (
    "run-integration",
    "run-release-test",
    "TEAM_ASSERT|",
    "self-hosted",
    "TEAM_FLOW_RUNNER",
    "role: test",
    "observation_digest",
    "destructive-confirmation",
    "persistent",
    "does not prove a fresh installation",
)
for token in required:
    self.assertIn(token, combined_docs)

for obsolete in ("TEAM_TEST_HISTORY_JSON", "test-plan.json", "apply-report.json"):
    self.assertNotIn(obsolete, operator_docs)
```

Do not apply obsolete-token assertions to historical design/plan documents;
those intentionally record prior states.

- [ ] **Step 2: Run docs tests and confirm they fail on old operator instructions**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_docs -v
```

Expected: FAIL until current operator docs match the final interfaces.

- [ ] **Step 3: Rewrite the normal-flow documentation**

Document these exact paths:

```text
Daily Builder work:
  edit -> export-app -> review -> commit -> push -> offline CI

Manual integration:
  protected runner -> run-integration -> qualification report

Release test:
  build -> download/verify -> run-release-test -> sign evidence -> gen-runbook

Destructive maintenance:
  dry-run -> human reviews exact confirmation -> explicit migrate/undo/redo

Recovery:
  inspect retained run token and evidence -> explicit recovery command
```

State that `run-integration` and `run-release-test` perform non-production
writes, while `qualify-target` remains the read-only diagnostic primitive.
Document the prepared-runner contract, observed version preflight, materialized
secret-file lifecycle, exact test-role signing requirement, payload-derived
release manifest, and accepted after-inventory digest.

- [ ] **Step 4: Run the full offline verification suite**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -q
uv run --no-project --with 'ruff>=0.5' ruff check scripts/
shellcheck scripts/*.sh
pwsh -NoLogo -NoProfile -Command '$errors = @(); Get-ChildItem scripts -Filter *.ps1 | ForEach-Object { [void][System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$null, [ref]$errors) }; if ($errors.Count) { $errors | ForEach-Object { $_.ToString() }; exit 1 }; "PowerShell parse OK"'
python3 - <<'PY'
import json
from pathlib import Path

for path in sorted(Path(".").rglob("*.json")):
    if any(part in {".git", "scratch", ".sync-state"} for part in path.parts):
        continue
    json.loads(path.read_text(encoding="utf-8"))
print("JSON OK")
PY
git diff --check
```

Expected: all commands exit 0; the unittest count is at least the pre-change
321 tests plus the new regressions.

- [ ] **Step 5: Perform protected non-production acceptance**

On the prepared integration runner, use a candidate containing one non-empty
PASS migration verification and one declared flow check:

```bash
PYTHONPATH=scripts python3 scripts/team.py \
  --env "$RUNNER_TEMP/integration.env" run-integration \
  --out "$RUNNER_TEMP/qualification.json"
```

Expected: PASS evidence with the observed toolchain digest and latest accepted
after-inventory digest.

On an isolated test target, exercise a disposable review migration whose
verification deliberately returns `FAIL`:

```bash
PYTHONPATH=scripts python3 scripts/team.py \
  --env "$RUNNER_TEMP/test.env" run-release-test \
  scratch/release/release.tar --target targets/test.json \
  --out "$RUNNER_TEMP/test-evidence.json"
```

Expected: exit 3, no APPLIED event for the failing migration, a `FAILED`
attempt, a retained mutex/recovery token, and no signable PASS evidence. Recover
only after the target owner reviews the observed state and supplies worker
termination evidence.

- [ ] **Step 6: Verify the final diff and repository boundary**

```bash
git status --short
git diff --stat
git diff --check
```

Confirm that no credentials, wallets, private keys, `.env` files, scratch
captures, `.sync-state` evidence, or live test artifacts are staged. Confirm the
docs still state that uncaptured/transient Builder edits and arbitrary DML are
outside the inventory boundary.

- [ ] **Step 7: Commit documentation and final verification updates**

```bash
git add README.md docs/ci.md docs/migrations.md docs/promotion.md \
  ci/app-checks/README.md \
  docs/superpowers/specs/2026-09-10-staging-qualification-and-undo-design.md \
  scripts/tests/test_docs.py
git commit -m "docs: describe simplified qualification workflow"
```

Do not push until the user or repository owner explicitly requests it.
