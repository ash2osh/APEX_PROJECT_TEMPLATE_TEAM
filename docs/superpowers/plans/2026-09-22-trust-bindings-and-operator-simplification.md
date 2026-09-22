# Trust Bindings and Operator Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind protected qualification evidence to the exact release and Git source bytes it claims, then make the safe daily, migration, integration, and release workflows easier for operators to discover and execute.

**Architecture:** Introduce one validated application-check bundle abstraction that can be built from either an immutable release archive or an exact Git commit, and pass that bundle through qualification instead of reopening the mutable checkout. Introduce one immutable integration-source snapshot keyed by a single resolved commit, then use its migration, canonical-inventory, application, and check bytes throughout the protected integration run. Keep signing, destructive confirmation, recovery, and production-owner handoff separate, while improving CLI help and generating reviewable false confirmation documents without approving them.

**Tech Stack:** Python 3.10+ standard library, `unittest`, Oracle SQL/PLSQL through qualified SQLcl 26.2.1+, APEX 26.1+, Oracle Database 23ai+, Git, GitHub Actions, Bash, PowerShell, Ruff, ShellCheck, and `cryptography>=41` for Ed25519 evidence signing and verification.

**Spec:** `docs/superpowers/specs/2026-09-10-repo-review-p1-remediation-and-flow-simplification-design.md` (corrective conformance plan based on the 2026-09-22 repository review)

## Global Constraints

- Preserve the shared-Builder contract: the daily loop is Builder edit, `export-app`, complete review, explicit staging, commit, pull/rebase, and push; there is no daily import.
- `import-app` remains a coordinated whole-application overwrite requiring a verified baseline/receipt, an independently posted team pause, exact source, and verified re-export.
- Production-classified setup, migration, recovery, deployment, import, and release writes remain refused before SQLcl starts.
- Database writes continue to use the isolated `METADATA` profile; observations and assertions continue to use the read-only `VERIFY` profile.
- Preserve target identity, drift, frontier, mutex, attempt, verification, recovery-token, archive-verification, signature, and production-owner boundaries.
- Never generate rollback SQL, affirmative destructive confirmation, credentials, commits, pushes, imports, or production writes automatically.
- Release checks must execute from verified archive bytes; integration checks, migrations, applications, and canonical inventory must come from one exact Git commit.
- Do not claim fresh installation or full runtime coverage. Persistent integration/test evidence remains observational, and uncaptured Builder edits and arbitrary DML remain outside the observed boundary.
- Keep the default push/pull-request workflow offline. Live Oracle/APEX acceptance runs only on prepared protected non-production runners.
- Preserve compatibility for existing low-level commands unless this plan explicitly changes an argument. The only intentional CLI contract change is that `sign-test-evidence` gains a required `--archive` argument.

## Review Focus

- A release whose checkout check files differ from the archive must execute the archive check declarations and SQL members, and signing/runbook generation must reject any digest mismatch.
- A dirty, untracked, or concurrently moved integration checkout must never cause working-tree migrations, inventory, applications, or checks to be attributed to the captured `source_commit`.
- Missing, extra, malformed, path-traversing, symlink-derived, non-UTF-8, or alias-mismatched check-bundle members must fail before an assertion or browser adapter runs.
- `--confirmation-out` must never create an affirmative document, overwrite a symlink, or leave a partial file; it must refuse when the dry run has no destructive confirmation requirement.
- Help/docs/workflows must describe only executable current paths, include untracked files in daily review, preserve cleanup on failure, and serialize protected release-test use of the persistent target.

---

## File Structure

### New files

- `scripts/teamlib/source_snapshot.py` — read and validate the exact Git commit inputs used by protected integration, and materialize only those verified bytes into bounded scratch space.
- `scripts/tests/test_source_snapshot.py` — exact-commit, dirty-worktree, path, mode, and missing-input regressions for the integration snapshot.

### Modified files

- `scripts/teamlib/app_checks.py` — own the normalized `AppCheckBundle` contract and validate declarations plus referenced members.
- `scripts/teamlib/release.py` — expose verified archive check members/digests on `Manifest` and construct an `AppCheckBundle` from archive bytes.
- `scripts/teamlib/qualification.py` — accept an explicit check bundle, run SELECT checks from bundle bytes, and validate evidence against a verified release during signing.
- `scripts/teamlib/fingerprints.py` — parse canonical inventory directly from exact Git blob bytes.
- `scripts/teamlib/evidence.py` — validate archive/source/check bindings in addition to the closed evidence shape.
- `scripts/teamlib/runbook.py` — re-derive the release check bundle and refuse signed evidence for another check set.
- `scripts/teamlib/online_workflows.py` — pass exact source snapshots and archive bundles through the two protected workflows.
- `scripts/teamlib/destructive_confirmation.py` — atomically write canonical false confirmation templates.
- `scripts/team.py` — add confirmation-output routing and descriptive command help without changing safety dispatch.
- `.github/workflows/release.yml` — serialize the persistent test target, remove unused OIDC permission, and pass the archive to signing.
- `README.md`, `.env.example`, `docs/ci.md`, `docs/migrations.md`, `docs/promotion.md`, `docs/toolchain.md`, `docs/design-review-resolution.md`, `docs/working-on-apex-together.html`, `ci/app-checks/README.md` — align current operator guidance with implemented commands and evidence boundaries.
- `scripts/tests/test_app_checks.py`, `scripts/tests/test_release.py`, `scripts/tests/test_qualification.py`, `scripts/tests/test_runbook.py`, `scripts/tests/test_online_workflows.py`, `scripts/tests/test_integration_workflow.py`, `scripts/tests/test_release_workflow.py`, `scripts/tests/test_destructive_confirmation.py`, `scripts/tests/test_launchers.py`, `scripts/tests/test_docs.py` — regressions and contract tests for the changes above.

---

### Task 1: Define One Validated Application-Check Bundle

**Files:**
- Modify: `scripts/teamlib/app_checks.py:27-311`
- Modify: `scripts/teamlib/release.py:28-42,198-203,324-466,469-545`
- Test: `scripts/tests/test_app_checks.py`
- Test: `scripts/tests/test_release.py`

**Interfaces:**
- Consumes: raw check members keyed relative to `ci/app-checks/`, for example `employee.json` and `employee/objects.verify.sql`; selected application aliases.
- Produces: `AppCheckBundle(declarations, members, checks_digest, artifact_digest)`; `build_app_check_bundle(members, aliases)`; `release_app_check_bundle(release_tar)`; `Manifest.app_checks_digest`.

- [x] **Step 1: Write failing bundle validation tests**

Add focused tests that build one valid declaration and referenced SELECT member entirely in memory:

```python
from teamlib.app_checks import AppCheckError, build_app_check_bundle


def valid_check_members():
    return {
        "employee.json": json.dumps({
            "version": 1,
            "alias": "employee",
            "page_ids": [1],
            "checks": [
                {
                    "id": "objects",
                    "page_id": 1,
                    "kind": "select",
                    "verify_sql": "employee/objects.verify.sql",
                    "expected_objects": ["APP.EMPLOYEE"],
                },
                {
                    "id": "login",
                    "page_id": 1,
                    "kind": "flow",
                    "flow": "employee/login.flow.json",
                    "steps": [
                        {
                            "action": "navigate",
                            "path": "/ords/r/app/employee/home",
                            "expected_visible_text": "Employee",
                        }
                    ],
                },
            ],
        }, sort_keys=True).encode("utf-8"),
        "employee/objects.verify.sql": (
            "SELECT 'TEAM_ASSERT|employee_table_exists|' || "
            "CASE WHEN COUNT(*) = 1 THEN 'PASS' ELSE 'FAIL' END "
            "FROM user_tables WHERE table_name = 'EMPLOYEE';\n"
        ).encode("utf-8"),
        "employee/login.flow.json": b"{}\n",
    }


def test_bundle_closes_aliases_and_referenced_members(self):
    bundle = build_app_check_bundle(valid_check_members(), ("employee",))
    self.assertEqual(set(bundle.declarations), {"employee"})
    self.assertTrue(bundle.member_bytes("employee/objects.verify.sql").startswith(b"SELECT"))
    self.assertEqual(len(bundle.checks_digest), 64)
    self.assertEqual(len(bundle.artifact_digest), 64)


def test_bundle_rejects_missing_extra_and_unsafe_members(self):
    missing = valid_check_members()
    missing.pop("employee/objects.verify.sql")
    with self.assertRaisesRegex(AppCheckError, "referenced check member is missing"):
        build_app_check_bundle(missing, ("employee",))

    with self.assertRaisesRegex(AppCheckError, "unknown application"):
        build_app_check_bundle({**valid_check_members(), "other.json": b"{}"}, ("employee",))

    with self.assertRaisesRegex(AppCheckError, "safe relative path"):
        build_app_check_bundle({"../employee.json": b"{}"}, ("employee",))
```

Also cover:

- duplicate/case-colliding member names;
- non-bytes member values;
- malformed UTF-8/JSON declarations;
- declaration alias mismatch;
- absent SELECT or flow declaration;
- a referenced path that exists but is attached to another alias;
- stable digests independent of input mapping order; and
- a changed referenced SQL byte changing `artifact_digest` even when declaration JSON is unchanged.
- changed referenced flow bytes changing `artifact_digest`; and
- a referenced flow member that is not UTF-8 JSON.

- [x] **Step 2: Run the focused tests and confirm the contract is missing**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_app_checks -v
```

Expected: FAIL because `AppCheckBundle` and `build_app_check_bundle` do not exist.

- [x] **Step 3: Implement the bundle in `app_checks.py`**

Add this public immutable interface:

```python
@dataclass(frozen=True)
class AppCheckBundle:
    declarations: Mapping[str, Mapping[str, Any]]
    members: Mapping[str, bytes]
    checks_digest: str
    artifact_digest: str

    def member_bytes(self, relative: str) -> bytes:
        try:
            return self.members[relative]
        except KeyError as exc:
            raise AppCheckError(
                f"referenced check member is missing: {relative}"
            ) from exc


def build_app_check_bundle(
    members: Mapping[str, bytes], aliases: Sequence[str]
) -> AppCheckBundle:
    selected = _validate_bundle_aliases(aliases)
    validated_members = _validate_bundle_members(members)
    declarations = _normalize_bundle_declarations(validated_members, selected)
    _validate_bundle_references(validated_members, declarations)
    return AppCheckBundle(
        declarations=declarations,
        members=validated_members,
        checks_digest=_digest(declarations),
        artifact_digest=_member_digest(validated_members),
    )
```

Implement it with these exact rules:

1. `_validate_bundle_aliases` validates aliases with `_ALIAS_RE`, requires at least one alias and no duplicates, and returns a sorted tuple.
2. `_validate_bundle_members` validates every member as a POSIX relative path with no empty, `.`, `..`, backslash, absolute, or case-colliding component and returns a path-sorted dictionary.
3. `_normalize_bundle_declarations` requires one `<alias>.json` declaration for every alias and no declaration for an unknown alias, decodes each declaration as UTF-8 JSON, and normalizes it through the existing `_load_declaration` logic.
4. `_validate_bundle_references` requires every SELECT `verify_sql` and every flow `flow` member, with each referenced path beginning with `<alias>/`; SELECT members must be UTF-8 and pass the existing read-only verification-SQL validator, while flow members must be UTF-8 JSON objects.
5. `_member_digest` computes the SHA-256 of canonical sorted records shaped as `{"path": path, "length": len(data), "sha256": sha256(data).hexdigest()}` for every bundle member.
6. Deep-copy normalized declarations and byte values so later mutation of caller-owned input cannot change the bundle; return path-sorted mappings and do not write files.

Refactor `verify_candidate_apps` to call the same declaration normalization helper so bundle construction and execution cannot drift.

- [x] **Step 4: Expose verified archive checks from `release.py`**

Extend `Manifest` without changing manifest format version 1:

```python
app_checks_digest: str | None = None
```

Place this field after the existing `toolchain` field and before `staging_dir`.

Use keyword arguments in `_manifest_from_data` instead of positional construction so the new field cannot shift an existing value into the wrong slot.

Add:

```python
def release_app_check_bundle(release_tar: str | Path) -> AppCheckBundle:
    archive = Path(release_tar)
    if archive.is_symlink() or not archive.is_file():
        raise ReleaseError("release archive is not a regular file")
    archive_digest, members = _read_archive_bytes(archive)
    manifest = _verify_archive_members(archive, archive_digest, members)
    raw = {
        path.removeprefix("release/checks/apps/"): data
        for path, data in members.items()
        if path.startswith("release/checks/apps/")
    }
    bundle = build_app_check_bundle(raw, tuple(sorted(manifest.app_tree_digests)))
    expected = manifest.app_checks_digest
    archive_records = [
        record for record in manifest.payload
        if record["path"].startswith("release/checks/apps/")
    ]
    actual = hashlib.sha256(_canonical(archive_records)).hexdigest()
    if expected != actual:
        raise ReleaseError("release app-check bundle does not match manifest")
    return bundle
```

Catch `AppCheckError` and re-raise `ReleaseError` with the original diagnostic. Do not extract the tar to an uncontrolled directory.

- [x] **Step 5: Add archive-bundle regression tests**

In `test_release.py`, build a release containing one app, its declaration, SELECT member, and flow member. Assert:

```python
manifest = build_release(repo, commit, "1.2.3", output)
bundle = release_app_check_bundle(manifest.archive_path)
self.assertEqual(bundle.checks_digest, build_app_check_bundle(source_members, ("employee",)).checks_digest)
self.assertEqual(manifest.app_checks_digest, verify_release(manifest.archive_path).app_checks_digest)
```

Mutate each of these archive members while rebuilding the manifest payload hashes, then assert verification or bundle loading refuses:

- declaration JSON;
- referenced `.verify.sql`;
- referenced flow JSON;
- missing declaration;
- missing referenced member; and
- an extra alias declaration not represented in `app_tree_digests`.

- [x] **Step 6: Run focused suites**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest \
    scripts.tests.test_app_checks \
    scripts.tests.test_release -v
```

Expected: PASS.

- [x] **Step 7: Commit the bundle boundary**

```bash
git add scripts/teamlib/app_checks.py scripts/teamlib/release.py \
  scripts/tests/test_app_checks.py scripts/tests/test_release.py
git commit -m "fix: define verified application check bundles"
```

---

### Task 2: Bind Release Qualification, Signing, and Runbook Generation

**Files:**
- Modify: `scripts/teamlib/evidence.py:15-206`
- Modify: `scripts/teamlib/qualification.py:66-156,263-405,442-480`
- Modify: `scripts/teamlib/online_workflows.py:38-56,265-310,396-461`
- Modify: `scripts/teamlib/runbook.py:191-227,230-251`
- Modify: `scripts/team.py:160-165,596-635`
- Modify: `.github/workflows/release.yml:80-92`
- Test: `scripts/tests/test_qualification.py`
- Test: `scripts/tests/test_online_workflows.py`
- Test: `scripts/tests/test_runbook.py`
- Test: `scripts/tests/test_release_workflow.py`

**Interfaces:**
- Consumes: `AppCheckBundle` and `Manifest` from Task 1; canonical version-2 evidence.
- Produces: `validate_release_evidence_binding(evidence, archive_digest, source_commit, checks_digest)`; a required `check_bundle: AppCheckBundle` keyword on `qualify_target`; `sign_test_evidence(evidence, release_archive, private_key, signature_out)`; release workflow signing with `--archive`.

- [x] **Step 1: Write failing evidence-binding tests**

Add this validator contract to `test_qualification.py` and `test_runbook.py`:

```python
from teamlib.evidence import EvidenceError, validate_release_evidence_binding


def test_release_binding_rejects_another_check_set(self):
    evidence = valid_evidence()
    evidence["application_checks"]["checks_digest"] = "0" * 64
    with self.assertRaisesRegex(EvidenceError, "application checks do not match"):
        validate_release_evidence_binding(
            evidence,
            archive_digest="a" * 64,
            source_commit="b" * 40,
            checks_digest="c" * 64,
        )
```

Cover all three independent mismatches:

- evidence `archive_digest` versus verified archive digest;
- evidence `source_commit` versus manifest source commit; and
- evidence application `checks_digest` versus `AppCheckBundle.checks_digest`.

Change the existing runbook success fixture so the release contains actual check members and the evidence uses the derived bundle digest. Add a regression showing the previously accepted arbitrary `"c" * 64` digest is refused even with a valid Ed25519 signature.

- [x] **Step 2: Run the evidence and runbook tests and confirm failure**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest \
    scripts.tests.test_qualification \
    scripts.tests.test_runbook -v
```

Expected: FAIL because evidence is not compared with the archive check bundle.

- [x] **Step 3: Implement the shared release-evidence binding validator**

Add to `evidence.py`:

```python
def validate_release_evidence_binding(
    evidence: Mapping[str, Any],
    *,
    archive_digest: str,
    source_commit: str,
    checks_digest: str,
) -> None:
    _digest(archive_digest, "verified archive digest")
    _digest(checks_digest, "verified application checks digest")
    if evidence.get("archive_digest") != archive_digest:
        raise EvidenceError("test evidence is for a different release archive")
    if evidence.get("source_commit") != source_commit:
        raise EvidenceError("test evidence is for a different source commit")
    checks = evidence.get("application_checks")
    if not isinstance(checks, Mapping) or checks.get("checks_digest") != checks_digest:
        raise EvidenceError("test evidence application checks do not match the release archive")
```

Call this only after `validate_test_evidence` has established the closed shape.

- [x] **Step 4: Make qualification consume an explicit bundle**

Change the public signature:

```python
def qualify_target(
    repo: str | Path,
    config: Config,
    source_commit: str,
    aliases: Sequence[str],
    *,
    store: Any,
    work: str | Path,
    check_bundle: AppCheckBundle | None = None,
    release_archive: str | Path | None = None,
    apply_report: str | Path | Mapping[str, Any] | None = None,
    flow_executable: str | None = None,
    sql_runner: Callable = run_sqlcl,
    run_identity: Mapping[str, Any] | None = None,
    runner_contract: str | Path = "ci/runner-contract.json",
    runtime_report: RuntimeReport | None = None,
) -> dict[str, Any]:
```

For release qualification, require `check_bundle` and refuse before checks when it is absent. For integration and the low-level diagnostic during this task, construct a temporary worktree bundle by reading every configured declaration plus its referenced SELECT/flow members into bytes, then pass that object through the same core. Task 3 removes this worktree fallback once the exact Git snapshot exists.

Replace the current repo-bound `_select_runner` with:

```python
def _select_runner(
    *, profile: Any, bundle: AppCheckBundle, work: Path,
    run_sqlcl: Callable = run_sqlcl,
):
    def resolve(alias: str, check: Mapping[str, Any]) -> dict[str, Any]:
        relative = str(check.get("verify_sql", ""))
        try:
            text = bundle.member_bytes(relative).decode("utf-8")
        except (AppCheckError, UnicodeError) as exc:
            return {"status": "FAIL", "diagnostic": str(exc)}
        driver_root = Path(work) / "app-checks" / alias / str(check.get("id", "check"))
        driver_root.mkdir(parents=True, exist_ok=True)
        driver = driver_root / "verify.sql"
        driver.write_text(
            "SET DEFINE OFF\nSET HEADING OFF\nSET FEEDBACK OFF\nSET PAGESIZE 0\n"
            + text,
            encoding="utf-8",
            newline="\n",
        )
        try:
            result = run_sqlcl(profile, "read", driver, driver_root)
        except Exception as exc:
            return {"status": "FAIL", "diagnostic": f"verification query failed: {exc}"}
        try:
            names = parse_team_assertions(getattr(result, "stdout", ""))
        except AssertionVerificationError as exc:
            return {"status": "FAIL", "diagnostic": str(exc)}
        return {"status": "PASS", "diagnostic": "", "assertions": list(names)}

    return resolve
```

Change `_flow_runner` to accept the same bundle:

```python
def _flow_runner(executable: str, work: Path, bundle: AppCheckBundle):
    def resolve(alias: str, check: Mapping[str, Any]) -> dict[str, Any]:
        relative = str(check.get("flow", ""))
        try:
            raw = bundle.member_bytes(relative)
            flow = json.loads(raw.decode("utf-8"))
        except (AppCheckError, UnicodeError, json.JSONDecodeError) as exc:
            return {"status": "FAIL", "diagnostic": f"flow member is unreadable: {exc}"}
        if not isinstance(flow, Mapping):
            return {"status": "FAIL", "diagnostic": "flow member must be a JSON object"}
        payload_root = Path(work) / "flow" / alias / str(check.get("id", "check"))
        payload_root.mkdir(parents=True, exist_ok=True)
        flow_path = payload_root / "flow.json"
        flow_path.write_bytes(raw)
        payload = payload_root / "check.json"
        executable_check = dict(check)
        executable_check["flow"] = str(flow_path)
        payload.write_text(
            json.dumps(executable_check, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
        return _invoke_flow_adapter(executable, alias, payload)

    return resolve
```

Extract the current subprocess/JSON-result logic into `_invoke_flow_adapter`. The adapter must receive only the rewritten bounded `flow.json` path; it must never receive a repo-relative path that could resolve to mutable checkout bytes. Add a regression that changes the checkout flow member after bundle construction and asserts the adapter receives the original bundle bytes.

Require `set(active_bundle.declarations) == set(selected)`. Pass `active_bundle.declarations` to `verify_candidate_apps`, and require `app_report.checks_digest == active_bundle.checks_digest` before producing PASS evidence. The release path must set `active_bundle = check_bundle` and may never select the worktree fallback.

- [x] **Step 5: Thread the archive bundle through `run-release-test`**

Extend `OnlineDependencies` with:

```python
release_app_checks: Callable[[Path], AppCheckBundle]
```

In `run_release_test`, load the bundle immediately after `verify_release`, before preflight or writes:

```python
manifest = deps.verify_release(archive_path)
check_bundle = deps.release_app_checks(archive_path)
report = deps.qualify_release(
    repo_path,
    config,
    source_commit,
    config_aliases,
    release_archive=archive_path,
    apply_report=apply_document,
    check_bundle=check_bundle,
    flow_executable=flow_executable,
    runtime_report=runtime,
)
```

Update all dependency fakes to record the bundle object and assert the release path never calls a checkout declaration loader.

- [x] **Step 6: Require the archive when signing**

Change the function and CLI contracts:

```python
def sign_test_evidence(
    evidence: str | Path,
    release_archive: str | Path,
    private_key: str | Path,
    signature_out: str | Path,
) -> str:
    raw, _ = _load_json(evidence, "test evidence")
    try:
        document = validate_test_evidence(raw)
        manifest = verify_release(release_archive)
        bundle = release_app_check_bundle(release_archive)
        validate_release_evidence_binding(
            document,
            archive_digest=manifest.archive_digest,
            source_commit=manifest.source_commit,
            checks_digest=bundle.checks_digest,
        )
    except (EvidenceError, ReleaseError) as exc:
        raise QualificationError(str(exc)) from exc
    key = _private_key(_regular_file(private_key, "signing key").read_bytes())
    destination = Path(signature_out)
    if destination.is_symlink():
        raise QualificationError(f"signature output must not be a symlink: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    signature = key.sign(raw)
    fd, name = tempfile.mkstemp(prefix=".evidence-signature-", dir=str(destination.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(signature)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise QualificationError(f"could not write evidence signature: {destination}") from exc
    return hashlib.sha256(raw).hexdigest()
```

Add required CLI argument:

```text
sign-test-evidence --evidence FILE --archive release.tar --private-key KEY --out SIG
```

Update `release.yml` to pass `--archive scratch/release/release.tar`. Tests must show no signature file is created for a mismatch.

- [x] **Step 7: Repeat the same binding at production handoff**

In `gen_runbook`, after verifying the archive and canonical evidence, derive the archive check bundle and call `validate_release_evidence_binding` before verifying the signature and before planning production:

```python
manifest = verify_release(release_tar)
bundle = release_app_check_bundle(release_tar)
evidence = validate_test_evidence(evidence_bytes)
validate_release_evidence_binding(
    evidence,
    archive_digest=manifest.archive_digest,
    source_commit=manifest.source_commit,
    checks_digest=bundle.checks_digest,
)
```

Keep signature verification mandatory; the digest binding does not replace it.

- [x] **Step 8: Run focused qualification, workflow, and runbook suites**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest \
    scripts.tests.test_qualification \
    scripts.tests.test_online_workflows \
    scripts.tests.test_runbook \
    scripts.tests.test_release_workflow \
    scripts.tests.test_launchers -v
```

Expected: PASS, with the native PowerShell launcher test skipped only when PowerShell is unavailable.

- [x] **Step 9: Commit the complete release-evidence binding**

```bash
git add scripts/teamlib/evidence.py scripts/teamlib/qualification.py \
  scripts/teamlib/online_workflows.py scripts/teamlib/runbook.py scripts/team.py \
  .github/workflows/release.yml scripts/tests/test_qualification.py \
  scripts/tests/test_online_workflows.py scripts/tests/test_runbook.py \
  scripts/tests/test_release_workflow.py scripts/tests/test_launchers.py
git commit -m "fix: bind promotion evidence to archived checks"
```

---

### Task 3: Use One Exact Git Snapshot for Protected Integration

**Files:**
- Create: `scripts/teamlib/source_snapshot.py`
- Create: `scripts/tests/test_source_snapshot.py`
- Modify: `scripts/teamlib/fingerprints.py`
- Modify: `scripts/teamlib/online_workflows.py:38-56,79-93,161-239,294-393`
- Modify: `scripts/team.py:355-390`
- Modify: `scripts/tests/test_online_workflows.py`
- Modify: `scripts/tests/test_integration_workflow.py`

**Interfaces:**
- Consumes: repository path, one already-resolved 40-character commit, configured aliases.
- Produces: `IntegrationSource(commit, migrations, canonical_inventory, app_trees, check_bundle)`; `load_integration_source(repo, commit, aliases)`; `IntegrationSource.materialize_migrations(root)`.

- [x] **Step 1: Write exact-source snapshot tests**

Create a temporary Git repository with committed migrations, `database/schema-inventory.json`, application source, and app checks. After committing, modify the migration, inventory, app, declaration, and SELECT SQL in the worktree and add an untracked migration. Assert the snapshot still contains only committed bytes:

```python
source = load_integration_source(repo, commit, ("employee",))
self.assertEqual(source.commit, commit)
self.assertEqual(source.migrations[f"{migration_id}.sql"], committed_sql)
self.assertEqual(source.canonical_inventory.as_dict(), json.loads(committed_inventory))
self.assertEqual(source.app_trees["employee"]["application.apx"], committed_app)
self.assertEqual(
    source.check_bundle.member_bytes("employee/objects.verify.sql"),
    committed_check_sql,
)
self.assertNotIn(f"{untracked_id}.sql", source.migrations)
```

Add refusals for:

- a symbolic or malformed commit input rather than the already-resolved exact SHA;
- missing canonical inventory;
- missing configured application;
- missing migration verification pair;
- unsupported Git mode or symlink under an owned prefix;
- a path outside `migrations/`, `database/schema-inventory.json`, `apps/<alias>/`, or `ci/app-checks/` leaking into the snapshot; and
- HEAD changing after `source_commit` is captured while the loaded snapshot remains stable.

- [x] **Step 2: Run the new suite and confirm the module is absent**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_source_snapshot -v
```

Expected: FAIL because `teamlib.source_snapshot` does not exist.

- [x] **Step 3: Implement immutable Git blob reading**

Create `source_snapshot.py` with no SQLcl or database dependency. Use `git ls-tree -r -z --full-tree <commit> -- <owned paths>` plus `git cat-file blob <oid>` with argv arrays. Refuse non-blob entries and modes outside `100644`/`100755`, unsafe paths, duplicate/case-colliding paths, and any resolved commit different from the supplied 40-character SHA.

Define:

```python
@dataclass(frozen=True)
class IntegrationSource:
    commit: str
    migrations: Mapping[str, bytes]
    canonical_inventory: Inventory
    app_trees: Mapping[str, Mapping[str, bytes]]
    check_bundle: AppCheckBundle

    @contextmanager
    def materialize_migrations(self, root: str | Path) -> Iterator[Path]:
        root_path = Path(root)
        root_path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".integration-migrations-", dir=str(root_path)
        ) as directory:
            destination = Path(directory)
            for name, data in sorted(self.migrations.items()):
                if Path(name).name != name:
                    raise SourceSnapshotError(f"unsafe migration member: {name}")
                (destination / name).write_bytes(data)
            load_bundles(destination)
            yield destination


def load_integration_source(
    repo: str | Path, commit: str, aliases: Sequence[str]
) -> IntegrationSource:
    owned = _read_owned_git_blobs(repo, commit, aliases)
    migrations = _migration_members(owned)
    load_bundles_from_bytes(migrations)
    inventory_bytes = _required_blob(owned, "database/schema-inventory.json")
    inventory = load_inventory_bytes(
        inventory_bytes,
        source=f"<git:{commit}:database/schema-inventory.json>",
    )
    app_trees = _application_trees(owned, aliases)
    check_members = _check_members(owned)
    check_bundle = build_app_check_bundle(check_members, aliases)
    return IntegrationSource(
        commit=commit,
        migrations=migrations,
        canonical_inventory=inventory,
        app_trees=app_trees,
        check_bundle=check_bundle,
    )
```

Implement the named private helpers in the same module. `load_bundles_from_bytes` must use a bounded temporary directory and the existing `load_bundles` parser; it returns the validated bundle mapping and never writes beneath the repository's `migrations/` directory.

`materialize_migrations` must:

1. create a new temporary directory beneath the caller's bounded scratch root;
2. write only direct migration member names from the immutable mapping;
3. validate the materialized directory with `load_bundles` before yielding it;
4. retain it only for the dry-run/apply pair; and
5. remove it on success or exception without touching any sibling scratch evidence.

Add `load_inventory_bytes(raw, source)` to `fingerprints.py`, with `load_inventory(path)` delegating to it. The loader must parse the bytes supplied by Git and retain the supplied source label in diagnostics; do not write the committed inventory into `database/`.

- [x] **Step 4: Pass the captured commit into migration application**

Change the dependency signature from:

```python
apply_migrations(repo, config, store, before_digest)
```

to:

```python
apply_migrations(repo, config, store, before_digest, source)
```

where `source` is the `IntegrationSource`. Remove `_resolve_head(repo)` from `_apply_migrations`; the function must use `source.commit` in migration evidence and `source.materialize_migrations(repo / "scratch" / "integration")` for both dry-run and apply:

```python
with source.materialize_migrations(repo / "scratch" / "integration") as migration_root:
    preview = apply_plan(
        migration_root,
        _migration_profiles(
            repo, config, store, dry_run=True,
            before_digest=before_digest,
            source_commit=source.commit,
        ),
    )
    if preview.confirmation_template is not None:
        return preview
    return apply_plan(
        migration_root,
        _migration_profiles(
            repo, config, store, dry_run=False,
            before_digest=before_digest,
            source_commit=source.commit,
        ),
        expected_plan={"pending": preview.selected},
    )
```

- [x] **Step 5: Use the same snapshot for drift, applications, and checks**

In `run_integration`, create the snapshot immediately after resolving HEAD:

```python
source_commit = deps.resolve_head(repo_path)
source = deps.load_source(repo_path, source_commit, aliases)
runtime = deps.preflight(config, repo_path, flow_executable)
```

Then replace the source-dependent calls after the existing controller setup, metadata bootstrap, inventory capture, and frontier checks with:

```python
deps.check_drift(source.canonical_inventory, before)
report = deps.apply_migrations(repo_path, config, store, before.digest, source)
deps.deploy_apps(repo_path, config, source)
deps.qualify_integration(
    repo_path,
    config,
    source.commit,
    aliases,
    store=store,
    check_bundle=source.check_bundle,
    flow_executable=flow_executable,
    runtime_report=runtime,
)
```

Change deployment to consume `source.app_trees[alias]` rather than reopening Git. Change drift comparison to consume the parsed committed inventory. The workflow must not read `repo/migrations`, `repo/database/schema-inventory.json`, `repo/apps`, or `repo/ci/app-checks` after the snapshot is created.

Immediately before the first metadata write, resolve HEAD again and refuse if it differs from `source.commit`. A later HEAD move does not change the loaded bytes, but record a diagnostic in the final report or failure output rather than silently claiming the checkout remained unchanged.

- [x] **Step 6: Make low-level `qualify-target` exact-source too**

When `team.py qualify-target` validates `--source-commit`, load `IntegrationSource` for the selected aliases and pass `source.check_bundle`. Preserve the command's read-only database behavior. Refuse when the checkout cannot resolve or load that exact commit; do not fall back to working-tree check files. After this step, make `qualify_target`'s `check_bundle` argument required and delete the temporary worktree fallback introduced in Task 2.

- [x] **Step 7: Add orchestration regression tests**

Update `test_online_workflows.py` dependency fakes so they receive the same source object at migration, deployment, and qualification boundaries. Assert:

```python
self.assertIs(calls["migrate_source"], calls["deploy_source"])
self.assertIs(calls["deploy_source"].check_bundle, calls["qualification_bundle"])
self.assertEqual(calls["migrate_source"].commit, manifest_source_commit)
```

Add one regression that modifies/untracks every working-tree source class after `load_source` returns and proves no callback observes those bytes.

- [x] **Step 8: Run exact-source neighboring suites**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest \
    scripts.tests.test_source_snapshot \
    scripts.tests.test_online_workflows \
    scripts.tests.test_integration_workflow \
    scripts.tests.test_migration_runner \
    scripts.tests.test_release_adapter \
    scripts.tests.test_trees -v
```

Expected: PASS.

- [x] **Step 9: Commit exact integration source selection**

```bash
git add scripts/teamlib/source_snapshot.py scripts/teamlib/fingerprints.py \
  scripts/teamlib/online_workflows.py scripts/team.py \
  scripts/tests/test_source_snapshot.py scripts/tests/test_online_workflows.py \
  scripts/tests/test_integration_workflow.py
git commit -m "fix: run integration from one exact Git snapshot"
```

---

### Task 4: Write Reviewable Destructive Confirmation Templates Safely

**Files:**
- Modify: `scripts/teamlib/destructive_confirmation.py:1-179`
- Modify: `scripts/team.py:99-113,296-309,412-440`
- Modify: `scripts/tests/test_destructive_confirmation.py`
- Modify: `scripts/tests/test_integration_workflow.py`
- Modify: `scripts/tests/test_launchers.py`

**Interfaces:**
- Consumes: a dry-run `confirmation_template` containing only `confirmed: false` entries.
- Produces: `write_confirmation_template(template, destination)` and CLI `--confirmation-out FILE` for `migrate`, `undo-migration`, and `redo-migration` dry runs.

- [x] **Step 1: Write atomic-output failure tests**

Add tests asserting:

```python
path = root / "confirmation.json"
write_confirmation_template(template, path)
self.assertEqual(json.loads(path.read_text()), template)
self.assertTrue(all(
    item["confirmed"] is False
    for item in json.loads(path.read_text())["confirmations"]
))
self.assertTrue(path.read_bytes().endswith(b"\n"))
```

Also assert refusal for:

- any entry with `confirmed: true`;
- a malformed/noncanonical template shape;
- a symlink destination;
- an existing destination, unless the bytes are already exactly identical;
- a destination whose parent is a symlink;
- `--confirmation-out` without `--dry-run`; and
- `--confirmation-out` when the dry run has no destructive requirement.

- [x] **Step 2: Run focused tests and confirm the writer is missing**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_destructive_confirmation -v
```

Expected: FAIL because `write_confirmation_template` does not exist.

- [x] **Step 3: Implement the canonical atomic writer**

Add:

```python
def write_confirmation_template(
    template: Mapping[str, Any], destination: str | Path
) -> Path:
    document, entries = _validate_document(template)
    if not entries:
        raise ConfirmationError("no destructive confirmation is required")
    if any(confirmed is not False for _key, confirmed in entries):
        raise ConfirmationError("confirmation template must remain unconfirmed")
    encoded = _canonical_with_lf(document)
    return _atomic_create_confirmation(encoded, destination)
```

Add `_atomic_create_confirmation(encoded, destination) -> Path` in the same module. It validates every existing parent with `lstat`, refuses symlinks, accepts an existing regular destination only when its bytes already equal `encoded`, otherwise uses `tempfile.mkstemp` in the destination directory, writes and `fsync`s, creates the final path without replacing a different existing file, `fsync`s the parent directory, and removes its temporary file on error.

Reuse the module's existing closed-shape and digest validation rather than creating a second schema definition.

- [x] **Step 4: Add CLI routing**

Add `--confirmation-out` to all three lifecycle parsers. Change `_migration_output` to accept the path and, after a successful dry run, call the writer only when a template exists. Include `confirmation_path` in stdout JSON when written; continue including the full false template for automation compatibility.

Required behavior:

```text
team.py migrate --dry-run --confirmation-out scratch/confirmation.json
```

must write the false document. This must refuse:

```text
team.py migrate --confirmation-out scratch/confirmation.json
```

The operator still changes only `confirmed: false` to `true` after review; the tool never performs that edit.

- [x] **Step 5: Run lifecycle and launcher suites**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest \
    scripts.tests.test_destructive_confirmation \
    scripts.tests.test_integration_workflow \
    scripts.tests.test_launchers \
    scripts.tests.test_migration_runner -v
```

Expected: PASS.

- [x] **Step 6: Commit the confirmation-output simplification**

```bash
git add scripts/teamlib/destructive_confirmation.py scripts/team.py \
  scripts/tests/test_destructive_confirmation.py \
  scripts/tests/test_integration_workflow.py scripts/tests/test_launchers.py
git commit -m "feat: write reviewable migration confirmation templates"
```

---

### Task 5: Make the CLI Self-Describing Without Hiding Low-Level Tools

**Files:**
- Modify: `scripts/team.py:73-165`
- Modify: `scripts/tests/test_launchers.py`
- Modify: `scripts/tests/test_integration_workflow.py`

**Interfaces:**
- Consumes: the existing command registry and dispatch behavior.
- Produces: top-level categorized command help, one-line descriptions for every command, and detailed epilogs for the normal daily/integration/release paths.

- [x] **Step 1: Write help-output tests**

Capture `_parser().format_help()` and command help. Require these category labels and representative commands:

```python
help_text = team._parser().format_help()
for label in (
    "Daily application work",
    "Protected qualification",
    "Migration maintenance",
    "Recovery and diagnosis",
    "Release and handoff",
):
    self.assertIn(label, help_text)

for command in (
    "export-app", "run-integration", "migrate",
    "recover-migration", "build-release", "gen-runbook",
):
    self.assertRegex(help_text, rf"{command}\s+\S")
```

Also assert:

- `export-app --help` says it captures the shared Builder application and may require reconciliation;
- `import-app --help` says it overwrites the shared application and requires a posted pause;
- `run-integration --help` says it performs non-production writes;
- `qualify-target --help` says it is read-only diagnosis;
- lifecycle help describes `--confirmation-out`; and
- `sign-test-evidence --help` shows required `--archive`.

- [x] **Step 2: Run help tests and confirm descriptions are absent**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_launchers -v
```

Expected: FAIL because current subparsers have no help descriptions or categories.

- [x] **Step 3: Implement a declarative command-help registry**

Add one mapping beside the privilege sets:

```python
COMMAND_HELP = {
    "doctor": ("Daily application work", "validate the selected credential-free target profile"),
    "export-app": ("Daily application work", "capture and reconcile the shared Builder application"),
    "import-app": ("Recovery and diagnosis", "coordinated overwrite of the paused shared Builder application"),
    "run-integration": ("Protected qualification", "apply and qualify one exact commit on protected integration"),
    "qualify-target": ("Protected qualification", "read-only diagnosis of a persistent qualified target"),
    "migrate": ("Migration maintenance", "preview or apply reviewed forward migration bundles"),
    "undo-migration": ("Migration maintenance", "preview or apply one global-LIFO authored down bundle"),
    "redo-migration": ("Migration maintenance", "preview or reapply one explicitly reverted bundle"),
    "recover-migration": ("Recovery and diagnosis", "clear a retained migration mutex from reviewed evidence"),
    "build-release": ("Release and handoff", "build and self-verify one immutable release archive"),
    "run-release-test": ("Release and handoff", "apply and qualify one archive on the protected test target"),
    "sign-test-evidence": ("Release and handoff", "bind and sign canonical PASS evidence for one archive"),
    "gen-runbook": ("Release and handoff", "verify signed evidence and generate the production-owner handoff"),
    "setup-state": ("Daily application work", "initialize shared application controller metadata"),
    "register-app": ("Daily application work", "register this checkout for a configured shared application"),
    "app-status": ("Daily application work", "read current shared application controller status"),
    "capture-app": ("Daily application work", "capture the shared Builder application without reconciliation"),
    "bootstrap-app": ("Daily application work", "establish the first verified application baseline"),
    "adopt-app": ("Daily application work", "adopt an existing shared application with evidence"),
    "resolve-export": ("Daily application work", "apply a reviewed export conflict resolution"),
    "announce-import": ("Recovery and diagnosis", "draft a pause or all-clear notice from observed evidence"),
    "deploy-app": ("Protected qualification", "deploy exact committed application bytes to a qualified target"),
    "adopt-frontier": ("Migration maintenance", "adopt a sequence-zero observed schema frontier"),
    "check-drift": ("Migration maintenance", "compare live schema structure and accepted frontier"),
    "export-history": ("Migration maintenance", "export canonical migration history from metadata"),
    "new-migration": ("Migration maintenance", "author a new forward migration and verification pair"),
    "add-dependency": ("Migration maintenance", "add an exact checksum dependency to a migration"),
    "migration-plan": ("Migration maintenance", "calculate dependency-ordered pending migrations offline"),
    "recover-app-lock": ("Recovery and diagnosis", "clear an application mutex from reviewed evidence"),
    "recover-files": ("Recovery and diagnosis", "finish or restore an interrupted file journal"),
    "explain-conflict": ("Recovery and diagnosis", "explain an immutable export conflict bundle"),
    "prune-scratch": ("Recovery and diagnosis", "prune bounded disposable scratch outputs"),
    "snapshot": ("Recovery and diagnosis", "build a canonical schema inventory from framed rows"),
    "verify-history": ("Recovery and diagnosis", "validate migration bundle history offline"),
    "replay": ("Recovery and diagnosis", "run deterministic migration replay against an explicit target"),
    "adopt-baseline": ("Recovery and diagnosis", "adopt reviewed replay output as a baseline"),
    "verify-release": ("Release and handoff", "verify immutable release archive bytes and manifest"),
    "plan-release": ("Release and handoff", "plan a verified archive against supplied target history"),
    "apply-release": ("Release and handoff", "apply a verified archive to a non-production target"),
    "ci-doctor": ("Release and handoff", "validate the offline runner contract shape"),
}
```

Build argparse help from this registry. Keep command names and parsing compatible. Add a test asserting `set(COMMAND_HELP)` equals the complete online/offline command set so a future command cannot ship without a description.

Use an argparse formatter/epilog to show three normal paths without adding another orchestration layer:

```text
Daily:      export-app -> review/stage -> commit -> pull/rebase -> push
Integration: run-integration
Release:    build/verify -> run-release-test -> sign-test-evidence -> gen-runbook
```

- [x] **Step 4: Verify all command help paths**

Run:

```bash
for command in $(PYTHONPATH=scripts python3 - <<'PY'
import team
print(" ".join(sorted(team.COMMAND_HELP)))
PY
); do
  PYTHONPATH=scripts python3 scripts/team.py "$command" --help >/dev/null
done
```

Expected: every command exits 0 without loading `.env` or contacting Oracle.

- [x] **Step 5: Commit CLI discoverability**

```bash
git add scripts/team.py scripts/tests/test_launchers.py \
  scripts/tests/test_integration_workflow.py
git commit -m "docs: make team workflow commands discoverable"
```

---

### Task 6: Harden Protected Release Serialization and Replace Stale Operator Guidance

**Files:**
- Modify: `.github/workflows/release.yml:7-12,37-116`
- Modify: `README.md:7-112`
- Modify: `.env.example:45-52`
- Modify: `docs/ci.md`
- Modify: `docs/migrations.md`
- Modify: `docs/promotion.md`
- Modify: `docs/toolchain.md`
- Modify: `docs/design-review-resolution.md`
- Modify: `docs/working-on-apex-together.html`
- Modify: `ci/app-checks/README.md`
- Modify: `scripts/tests/test_release_workflow.py`
- Modify: `scripts/tests/test_docs.py`

**Interfaces:**
- Consumes: the final CLI and trust contracts from Tasks 1-5.
- Produces: one current operator story; serialized protected test workflow; semantic documentation tests that reject retired commands and unsafe Git examples.

- [x] **Step 1: Write workflow hardening tests**

In `test_release_workflow.py`, require:

```python
self.assertIn("concurrency:", release)
self.assertIn("group: example-team-apex-test", release)
self.assertIn("cancel-in-progress: false", release)
self.assertNotIn("id-token: write", release)
self.assertIn("--archive scratch/release/release.tar", release)
```

Retain all existing assertions for exact checkout, self-hosted test runner, protected environment, secret-file mode, cleanup, signing order, report retention, and production handoff.

- [x] **Step 2: Write semantic operator-document tests**

Define `operator_docs` as current README, `.env.example`, non-historical Markdown under `docs/`, the HTML explainer, and `ci/app-checks/README.md`. Assert it does not contain:

```python
retired_or_unsafe = (
    "scripts/tests/live",
    "ci-replay",
    "CI therefore builds a disposable schema",
    "required by migration/replay commands",
    'git commit -am "Describe the Builder change"',
    "git diff -- apps/<alias>/ .sync-state/",
)
for token in retired_or_unsafe:
    self.assertNotIn(token, operator_docs)
```

Require the current daily loop to contain all of:

```python
for token in (
    "git status --short --untracked-files=all",
    "git add -- apps/<alias>/",
    "git diff --cached",
    "git pull --rebase",
    "git push",
):
    self.assertIn(token, readme)
self.assertNotIn("git add -A", readme)
```

Require current docs to show framed assertions containing `TEAM_ASSERT|` and to name the exact-source archive/Git check binding.

- [x] **Step 3: Run workflow/docs tests and confirm current drift**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest \
    scripts.tests.test_release_workflow \
    scripts.tests.test_docs -v
```

Expected: FAIL on missing release concurrency, unused OIDC permission, unsafe daily Git commands, nonexistent live-suite path, and retired disposable-replay language.

- [x] **Step 4: Serialize protected release-test usage**

Add at workflow top level:

```yaml
concurrency:
  group: example-team-apex-test
  cancel-in-progress: false
```

Remove `id-token: write`; no step consumes OIDC. Keep `contents: read`. Do not cancel an in-flight test run because its target may be between migration, APEX deployment, and qualification boundaries.

Pass the archive to `sign-test-evidence` exactly as defined in Task 2.

- [x] **Step 5: Correct the README daily loop**

Replace the current block with:

```text
scripts/team.sh export-app <alias>
git status --short --untracked-files=all -- apps/<alias>/ migrations/
git add -- apps/<alias>/
# If this Builder change needs a schema migration, stage its exact four members:
git add -- migrations/<migration-id>.sql migrations/<migration-id>.verify.sql \
  migrations/<migration-id>.down.sql migrations/<migration-id>.down.verify.sql
git diff --cached -- apps/<alias>/ migrations/
git commit -m "Describe the shared Builder and schema change"
git pull --rebase
git push
```

State explicitly:

- omit nonexistent optional down members rather than using a broad glob;
- an export is the shared application's observed state, not only the operator's edits;
- `.sync-state/` is durable local recovery evidence but intentionally ignored by Git;
- `git status` is required because `git diff` alone hides untracked exported components; and
- no tool automatically stages, commits, pulls, rebases, pushes, imports, or clears recovery state.

- [x] **Step 6: Replace retired documentation**

Make these exact corrections:

- `.env.example`: say VERIFY is optional for daily APEX capture and required by the commands in `VERIFY_REQUIRED_COMMANDS`; remove “Plan 1” and replay wording.
- `docs/toolchain.md`: remove the nonexistent `scripts/tests/live` command. Point protected online acceptance to `run-integration` and `run-release-test`, and keep offline portability commands separate.
- `docs/design-review-resolution.md`: label the 2026-09-07 Docker/disposable claims as historical evidence and add a current-state section naming persistent protected qualification and this remediation plan. Do not present deleted CI as current.
- `docs/working-on-apex-together.html`: replace disposable-schema language with persistent integration/test qualification limitations; change verification SQL examples to emit framed `TEAM_ASSERT|name|PASS|FAIL` rows; add the complete tracked/untracked review sequence.
- `ci/app-checks/README.md`: state that release checks and referenced SQL/flow members come from verified archive bytes, while integration checks come from one exact Git commit.
- `docs/ci.md` and `docs/promotion.md`: document archive-bound check execution, `--archive` signing, release concurrency, and the repeated runbook binding check.
- `docs/migrations.md`: document `--confirmation-out` and reiterate that the generated document remains false until a human changes only the confirmation booleans.

- [x] **Step 7: Run docs/workflow tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest \
    scripts.tests.test_release_workflow \
    scripts.tests.test_docs \
    scripts.tests.test_ci_contract -v
```

Expected: PASS.

- [x] **Step 8: Commit workflow and operator guidance**

```bash
git add .github/workflows/release.yml README.md .env.example \
  docs/ci.md docs/migrations.md docs/promotion.md docs/toolchain.md \
  docs/design-review-resolution.md docs/working-on-apex-together.html \
  ci/app-checks/README.md scripts/tests/test_release_workflow.py \
  scripts/tests/test_docs.py
git commit -m "docs: align the safe operator workflow"
```

---

### Task 7: Complete Offline Verification and Protected Acceptance

**Files:**
- Modify only if a verification-discovered defect requires a focused regression and fix.
- Record protected-run evidence outside Git under bounded runner output and `.sync-state/`; do not commit credentials, captures, signatures, or live reports.

**Interfaces:**
- Consumes: all prior task commits.
- Produces: reproducible offline verification plus explicit protected non-production acceptance or an honest list of unverified boundaries.

- [x] **Step 1: Run the full offline Python suite**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest discover -s scripts/tests -v
```

Expected: all tests pass; only the native PowerShell execution test may skip when PowerShell is unavailable. The count must exceed the pre-plan baseline of 409 tests.

- [x] **Step 2: Run static, shell, JSON, and whitespace gates**

```bash
uv run --no-project --with 'ruff>=0.5' ruff check scripts/
for script in scripts/*.sh; do bash -n "$script"; done
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

Expected: every available command exits 0. If ShellCheck or PowerShell is unavailable locally, do not mark it passed; run that gate in CI or a prepared environment before completion.

- [x] **Step 3: Prove the release-check mismatch now refuses offline**

Build a release fixture, create canonical PASS evidence whose `checks_digest` differs from the archive bundle, sign its bytes directly with a test key, and call `gen_runbook`. Expected:

```text
test evidence application checks do not match the release archive
```

Then use the correct digest and call `scripts/team.py sign-test-evidence --evidence test-evidence.json --archive release.tar --private-key test-key.pem --out test-evidence.sig`; expected: signature creation succeeds and `gen-runbook` accepts it.

- [x] **Step 4: Prove dirty integration source cannot leak offline**

Run the `test_source_snapshot` regression that commits source, changes every owned worktree input, adds an untracked migration, and verifies the snapshot and orchestration callbacks still receive only committed bytes. Expected: PASS.

- [ ] **Step 5: Run protected integration acceptance**

On the prepared integration runner, select a clean exact commit containing one non-empty PASS migration verification, one SELECT app check, and one flow check:

```bash
PYTHONPATH=scripts python3 scripts/team.py \
  --env "$RUNNER_TEMP/integration.env" run-integration \
  --out "$RUNNER_TEMP/qualification.json"
```

Expected: PASS report whose `source_commit` equals runner HEAD, whose application `checks_digest` equals the exact Git snapshot bundle, and whose observation digest is the accepted after-inventory digest. Confirm no working-tree source was read after snapshot creation.

- [ ] **Step 6: Run protected release-test acceptance**

On the protected test runner:

```bash
PYTHONPATH=scripts python3 scripts/team.py \
  --env "$RUNNER_TEMP/test.env" run-release-test \
  scratch/release/release.tar --target targets/test.json \
  --out "$RUNNER_TEMP/test-evidence.json"

PYTHONPATH=scripts python3 scripts/team.py sign-test-evidence \
  --evidence "$RUNNER_TEMP/test-evidence.json" \
  --archive scratch/release/release.tar \
  --private-key "$RUNNER_TEMP/test-signing-key.pem" \
  --out "$RUNNER_TEMP/test-evidence.sig"
```

Expected: PASS evidence and signature bound to the archive source commit, archive digest, protected test identity, accepted frontier, and archived app-check bundle.

Repeat with a deliberately mismatched checkout check declaration after the archive is built. Expected: release qualification still executes archive bytes and produces the same archive-derived digest. Repeat with tampered evidence. Expected: signing refuses and creates no signature.

- [x] **Step 7: Verify repository boundaries**

```bash
git status --short --branch
git diff --check
git ls-files | rg '(^|/)(\.env|scratch|\.sync-state)(/|$)' && exit 1 || true
git ls-files | rg '(private|signing).*key|\.pem$' && exit 1 || true
```

Confirm no credentials, wallets, private keys, `.env` files, scratch captures, `.sync-state` evidence, signatures, live qualification reports, or production data are staged.

- [x] **Step 8: Record the final verification truthfully**

Update this plan's checkbox state and append a short `## Implementation Verification` section containing:

- exact commit tested;
- offline test count and skip count;
- Ruff, Bash, ShellCheck, PowerShell, JSON, and whitespace results;
- protected integration run ID/result;
- protected release-test run ID/result;
- whether browser flow checks actually ran; and
- every unavailable or unverified boundary.

Do not convert an unavailable protected runner, Oracle/APEX target, ShellCheck, PowerShell, or browser adapter into a PASS claim.

- [x] **Step 9: Commit verification-only documentation if it changed**

```bash
git add docs/superpowers/plans/2026-09-22-trust-bindings-and-operator-simplification.md
git commit -m "docs: record trust binding verification"
```

Do not push until the repository owner explicitly requests it.

---

## Implementation Verification

- Exact implementation commit tested: `ec0b6d74c2e29176741c092d707c5233a7184b42`.
- Offline suite: 436 tests passed; 1 native PowerShell execution test skipped because PowerShell is unavailable. This exceeds the 409-test baseline.
- Static and repository gates: Ruff passed; Bash syntax passed; JSON parsing passed; `git diff --check` passed; tracked `.env`, scratch, `.sync-state`, private-key, signing-key, and PEM boundary checks passed.
- Unavailable local gates: ShellCheck was not installed, and PowerShell was not installed. Neither is recorded as passed.
- Release binding: the offline mismatch and correct-digest signature/runbook regressions passed. Mismatched application-check evidence is refused, and correct archive-bound evidence is accepted by the offline test fixture.
- Exact integration source: committed-byte, orchestration-order, and later-HEAD-change regressions passed; worktree changes and untracked migration input did not enter the protected source snapshot.
- Docker-demo: direct read-only identity inspection confirmed `DEMO` on `FREEPDB1`/`freepdb1`. The qualified read-only schema inventory adapter then timed out at its 120-second limit, so canonical drift and live inventory are unavailable. No database write operation was requested.
- Protected integration acceptance: not run; no run ID or PASS result exists. This template checkout has no configured application source, target binding, canonical schema inventory, or qualified flow runner.
- Protected release-test acceptance: not run; no run ID, evidence signature, or PASS result exists. A protected test environment, release fixture, flow adapter, and signing key were unavailable.
- Browser flow checks: not run; no configured application or browser-flow adapter was available.
- Safety boundary: no shared Builder import, production write, push, credential capture, or protected evidence was produced.

---

## Deferred Items

- Deleting `origin/codex/p1-remediation-flow-simplification` is not part of implementation. It is fully merged as of the review snapshot, but remote deletion requires exact-name confirmation and a final `git ls-remote` check.
- Automatic commits, pushes, imports, production deployment, affirmative destructive confirmation, generated rollback SQL, and automatic recovery remain explicitly out of scope.
- General decomposition of `migration_store.py` or other large modules is not required for these trust and operator-flow fixes; pursue it only under a separate behavior-preserving refactor plan.
