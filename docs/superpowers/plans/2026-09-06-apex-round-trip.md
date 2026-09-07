# APEX Team Round Trip — Implementation Plan (Plan 1 of 3)

> **For agentic workers:** Use superpowers:executing-plans, or
> superpowers:subagent-driven-development when delegation is authorized.
> Execute tasks with their tests; checkboxes below describe unfinished work.

**Revision:** 3 — separates export checkpoints, receipt absences and physical
lock identity.
**Goal:** Capture Builder changes without silently overwriting Git or local work.
**Architecture:** One Python core performs target validation, content reconciliation,
journaled file changes and verified imports. Bash and PowerShell are launchers.
Baselines and recovery captures retain exact bytes independently of Git history.
**Tech Stack:** Python 3.10+, Bash, PowerShell 5.1/7, Git, qualified SQLcl/APEX.
**Spec:** [Team design](../specs/2026-09-06-team-template-design.md), §§4–6, 8–11.

## Global constraints

All tasks inherit spec §8 verbatim as their safety/portability contract.
No database operation is authorized by executing a documentation example.
Preserve the solo repository. The TEAM directory initially contains only docs:
repository initialization is an implementation step, not already completed.
Do not commit/push unless delivery is authorized. Keep durable recovery in
.sync-state; temporary helpers belong in scratch.

The earlier exported-directory deletion, filename-only decision and
"import first" recovery examples have been removed. Do not restore them.

Follow spec §8 "Executable content in plans": pure offline modules
(config.py, trees.py, reconcile.py, patch.py, masters.py parsing) carry exact
signatures and runnable tests; anything reaching a database is a directive
behind the verified adapter, never a runnable command line. Where a task below
gives an interface but no test body, write the tests first from the listed
cases — an unlisted case is not thereby excluded.

## File responsibilities and shared contracts

| Files to create | Responsibility |
|---|---|
| scripts/team.py; scripts/teamlib/__init__.py | CLI and package |
| scripts/teamlib/config.py; targets/*.json; .env.example | literal config, expected identities and roles |
| scripts/teamlib/sqlcl.py; scripts/sql/identity.sql | process protocol and SELECT-only identity capture |
| scripts/teamlib/trees.py; scripts/teamlib/state.py | exact trees, ownership, baseline/capture receipts |
| scripts/teamlib/control_store.py; scripts/sql/control_metadata.sql | isolated shared metadata bootstrap, checkout registry and app mutex |
| scripts/teamlib/reconcile.py; scripts/teamlib/patch.py | pure decision and journaled application |
| scripts/teamlib/apex.py; scripts/teamlib/masters.py | export/import qualification and subscriptions |
| scripts/export_app.sh/.ps1; scripts/import_app.sh/.ps1 | compatibility launchers |
| scripts/team.sh; scripts/team.ps1 | complete command surface |
| scripts/tests/test_*.py; scripts/tests/fixtures/ | unit and public-entry-point tests |
| .github/workflows/template-checks.yml | database-free portability checks |
| docs/toolchain.md; docs/app-recovery.md | qualified versions and recovery contract |

Public commands (all accept --env as an alternative to PROJECT_ENV_FILE):

```text
team.py doctor
team.py setup-state
team.py register-app ALIAS [--transfer-from CHECKOUT_UUID]
team.py app-status ALIAS
team.py recover-app-lock ALIAS [--run-token TOKEN] --evidence DIRECTORY
team.py capture-app ALIAS
team.py bootstrap-app ALIAS
team.py adopt-app ALIAS --ref COMMIT
team.py export-app ALIAS
team.py resolve-export RECOVERY_ID --resolved DIRECTORY
team.py import-app ALIAS --ref COMMIT [--replace-from RECOVERY_ID]
team.py recover-files OPERATION_ID --action finish|restore
```

Developer import defaults --ref to HEAD, resolved once before I/O.
capture-app only captures, never imports or modifies tracked source.
app-status is read-only. It prints every checkout registered against the target
with its host, user and registration timestamp, plus any held app-target mutex
and the current import generation. For the shared development application that
list has many rows and is a roster, not an ownership claim (spec §9). For a
single-owner target it also makes the value `--transfer-from` needs discoverable
from the registry rather than from local state a re-clone may have lost.
`--transfer-from` applies only to single-owner targets and is rejected against
the shared application.
Exit codes: 0 verified success, 2 invalid contract/target, 3 conflict or
precondition refusal, 4 uncertain/incomplete operation, 5 external-tool failure.
Print a JSON result containing status, operation_id, changed_paths,
conflicts and recovery_path; keep sanitized diagnostics on stderr.

Core types, defined in their owning modules before consumers:

```python
# trees.py
from dataclasses import dataclass
from typing import Mapping
Tree = Mapping[str, bytes]                 # absence is not an empty file

# config.py
@dataclass(frozen=True)
class Target:
    project: str
    role: str
    environment: str
    connection: str
    instance_id: str
    db_name: str
    service: str
    session_user: str
    current_schema: str
    alias: str | None                      # None for tables/code/metadata/
                                            # verify profiles; required
                                            # wherever apps/<alias>/ resolves
    workspace_id: int | None
    app_id: int | None
    parsing_schema: str | None
    ownership_mode: str                    # "shared" | "single"
                                            # (from APP_OWNERSHIP_MODE)
    binding_digest: str

# reconcile.py
@dataclass(frozen=True)
class Decision:
    tree: dict[str, bytes]                 # candidate, incomplete on conflicts
    conflicts: tuple[str, ...]

# state.py
@dataclass(frozen=True)
class Baseline:
    version: int
    state_key: str                        # full local binding identity
    source_commit: str
    tree_digest: str
    blobs: dict[str, str]                  # path -> SHA-256

@dataclass(frozen=True)
class Checkpoint:
    version: int
    state_key: str
    captured: Tree                        # loaded from verified blob manifests
    reconciled: Tree
    original_head: str
    receipt_id: str
    anchor_commit: str | None              # bound before next clean export
```

## Task 1: Repository, config and target contracts

**Files:** .gitignore, .gitattributes, .env.example, targets/development.json,
targets/integration.json, targets/test.json, targets/controllers.json,
scripts/teamlib/config.py,
scripts/tests/test_config.py.

- [ ] Inspect parent instructions and existing files; initialize TEAM Git only
  during implementation, preserving all four documents.
- [ ] Define strict literal .env parsing in Python. Supported keys: PROJECT_NAME,
  TARGET_ROLE, DB_ENVIRONMENT, APEX_APPS; TABLES_SCHEMA, CODE_SCHEMA,
  APEX_PARSING_SCHEMA, METADATA_SCHEMA; for each profile prefix require
  SQLCL_CONNECTION, EXPECTED_USER, EXPECTED_CURRENT_SCHEMA,
  EXPECTED_DB_NAME, EXPECTED_SERVICE, EXPECTED_INSTANCE_ID; additionally
  APEX_WORKSPACE_ID and APP_OWNERSHIP_MODE (`shared` default, or `single` for
  an optional isolated developer copy per spec §2.1).
- [ ] Dispatch offline commands before `.env` loading or database-profile
  validation. `new-migration`, `add-dependency`, file-based `migration-plan`,
  `build-release`, `verify-release`, `plan-release`, `gen-runbook` and offline
  conflict explanation require only their explicit local inputs. They work with
  no `.env`, even if an unrelated PROJECT_ENV_FILE points to a missing file.
- [ ] Online Plan 1 commands load TABLES, CODE, APEX and METADATA as their core
  profile set. VERIFY is optional until a command requests postconditions:
  Plan 2 migrate/replay require it; check-drift uses tables/code plus metadata.
  A partially configured VERIFY profile in a loaded `.env` is an error. Mirror
  this in `.env.example` with VERIFY keys commented. Test offline commands with
  no configuration, online missing-profile errors, Plan 1 without VERIFY, and
  partial VERIFY refusal when the configuration is loaded.
  EXPECTED_INSTANCE_ID encodes the stable database/container identity obtained
  through a qualified identity query; all profiles within a shared schema set
  must match it independent of service aliases. No fallback connection.

- [ ] Enforce exact known keys, duplicates, empty required values, control
  characters, alias/path grammar and uppercase Oracle identifier contracts.
  Compare role and environment exactly. Permit equal tables/code profiles.
  Require METADATA_SCHEMA distinct from both application schemas. The metadata
  owner is the controller principal; VERIFY has read-only grants and owns no
  application/metadata objects. Setup checks effective roles/ANY/proxy access
  cannot bypass these boundaries before enabling writes.
- [ ] Reject mismatched deployment JSON and profile identities. Define target
  JSON schema with these same fields plus per-alias app IDs; secret values are
  prohibited. Local default binding is generated only by explicit setup.
- [ ] Define `targets/development.json` as the tracked contract spec §7
  requires for the shared development target: a named recovery-owner role (at
  least two people, never an individual), read by `recover-app-lock` and
  printed in every mutex-blocking refusal. Unlike `targets/integration.json`
  and `targets/test.json` it carries no app ID, workspace ID or binding — those
  stay in `.env` for the shared development target (spec §5) — so its schema
  is deliberately smaller and cannot duplicate or drift from `.env`.
- [ ] Add ignore rules for .env and .env.*, with !.env.example; .sync-state/,
  scratch/, dist/, apps/**/deployments/default.json. Add LF rules for .apx,
  .sql, .json, .sh and .ps1.
- [ ] Run this regression suite before implementation, then make it pass:

```python
# scripts/tests/test_config.py
import unittest
from teamlib.config import parse_apps

class ConfigTests(unittest.TestCase):
    def test_alias_mapping(self):
        self.assertEqual(parse_apps("checkout:101,admin:102"),
                         {"checkout": 101, "admin": 102})
    def test_reject_ambiguous_or_unsafe_mapping(self):
        for value in ("Checkout:101", "../x:101", "x:0",
                      "x:101,x:102", "x:101,y:101", "x:101, y:102"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_apps(value)
```

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_config.py -v`.
Add loader fixtures for CRLF, duplicate/missing keys and literal shell syntax;
assert a sentinel command in a value never executes.

## Task 2: One SQLcl process boundary

**Files:** scripts/teamlib/sqlcl.py, scripts/sql/identity.sql,
scripts/tests/test_sqlcl.py, scripts/tests/fixtures/fake_sqlcl.py.

**Interface:** `run_sqlcl(target: Target, operation: str, driver: Path,
work: Path) -> SqlResult`. SqlResult contains verified identity, sanitized log
path and parsed result manifest. operation is read or write.

- [ ] Read the solo launcher/guards/identity SQL and their tests. Port their
  lessons, not the incorrect calling convention: the old identity include
  requires target_schema, db_environment and expected_user definitions.
- [ ] Use a regular empty file as stdin, shell=False, an argv list, explicit
  working directory, UTF-8 and a generated @driver.sql file. Resolve Windows
  sql.exe/sql.bat launch behavior and quoting with native tests; paths containing
  spaces must work. Reject unsupported shell metacharacters explicitly.
- [ ] Perform SELECT-only identity discovery for every connection. Verify all
  expected fields, classify production-like identities, then verify again in
  the actual payload session before any source statement. Use a SQLcl driver
  mechanism qualified to stop on mismatch; do not let a Python preflight alone
  authorize a later unchecked session.
- [ ] Refuse production writes before launching SQLcl. Production read drivers
  contain SELECT statements only, without a PL/SQL assertion block.
- [ ] Keep metadata/verification sessions separate from payload sessions. Do
  not expose controller credentials or mutex tokens to payload processes. Use
  controlled startup/login scripts; reject unreviewed connection-changing or
  mutating startup behavior. Profile operation classes include metadata writes
  and verification reads, and verification writes always refuse.
- [ ] Set SQLERROR/OSERROR exits, DEFINE OFF for payloads and positive,
  operation-specific completion records. Reject ORA-/SP2-/SQLcl/Java failures
  even on exit 0, missing/truncated output and malformed records. A bare marker
  after an unsuccessful APEX command is insufficient: verify expected exports
  or post-import state.
- [ ] Test error-zero, startup exception, wrong identity, changed second-session
  identity, no completion, CRLF, Unicode and stdin regular-file type.

Executable subprocess contract to implement inside the adapter:

```python
with stdin_path.open("rb") as empty:
    result = subprocess.run(
        argv, stdin=empty, cwd=work, shell=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )
```

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_sqlcl.py -v`.

## Task 3: Exact trees and owned-source manifest

**Files:** scripts/teamlib/trees.py, scripts/tests/test_trees.py.

**Interfaces:** `read_git_tree(repo, commit, alias) -> dict[str, bytes]`;
`read_export_tree(path) -> dict[str, bytes]`; `tree_digest(tree) -> str`;
`assert_source_clean(repo, alias) -> None`; `tree_contains(subset: Tree,
superset: Tree) -> bool`; `receipt_satisfied(result: Tree, required_absent:
set[str], selected: Tree) -> bool`.

- [ ] Define ownership using a qualified full SQLcl export fixture. Include
  binaries and .apex/apexlang.json; exclude deployments/** and export logs.
  Unknown export classes fail rather than being silently dropped.
- [ ] Read Git paths with NUL-delimited plumbing and blob bytes, not locale
  text listings. Fail on unresolved commit/read failures. Preserve zero-byte
  files, normalize only APEXlang LF, reject symlinks, unsafe modes, traversal,
  case collisions and Windows-reserved components.
- [ ] Check index/worktree plus untracked and ignored collisions in the owned
  source set. Unrelated ignored files outside that set remain untouched.
- [ ] Digest sorted POSIX paths plus explicit byte lengths and file hashes;
  use the same canonical JSON manifest encoding everywhere.
- [ ] Implement `tree_contains` as a pure subset check, but never use it alone
  to authorize import. `receipt_satisfied` also requires every tombstone absent
  from selected source. Result paths and tombstones must be disjoint and safe.
  Spec §6 defines the cumulative required-absence set; preserve it across no-op
  exports and reject receipts without its versioned evidence.

```python
def tree_contains(subset, superset):
    return all(path in superset and superset[path] == value
               for path, value in subset.items())

def receipt_satisfied(result, required_absent, selected):
    if set(result) & set(required_absent):
        raise ValueError("receipt contains contradictory presence/absence")
    return (tree_contains(result, selected)
            and not set(required_absent).intersection(selected))
```

- [ ] Add fixtures for names containing spaces, binary zeroes, LF/CRLF,
  zero-byte-versus-missing, deleted/added Git files, corrupt refs and preserved
  deployment JSON. Confirm corrupted Git reads never become empty trees.
- [ ] Add these regression tests first. `reconcile` distinguishes a missing
  path from a zero-byte file, so a Tree that collapses the two silently
  destroys that distinction one layer down, where it is no longer observable:

```python
import unittest
from teamlib.trees import read_export_tree, tree_digest, tree_contains

class TreeSemanticsTests(unittest.TestCase):
    def test_zero_byte_file_is_present_not_absent(self):
        tree = read_export_tree(self.fixture("zero-byte"))
        self.assertIn("pages/p00001-home.apx", tree)
        self.assertEqual(tree["pages/p00001-home.apx"], b"")

    def test_absent_path_is_not_a_key(self):
        tree = read_export_tree(self.fixture("zero-byte"))
        self.assertNotIn("pages/p00002-missing.apx", tree)

    def test_zero_byte_and_absent_digest_differently(self):
        # The two states must never produce the same manifest digest.
        self.assertNotEqual(tree_digest({"a": b""}), tree_digest({}))

    def test_binary_bytes_are_exact(self):
        tree = read_export_tree(self.fixture("binary"))
        blob = tree["shared-components/static-files/icons/app-icon-32.png"]
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")

    def test_binary_is_not_line_ending_normalised(self):
        # LF normalisation applies to APEXlang only; touching binaries here
        # corrupts assets in a way no later stage can detect.
        tree = read_export_tree(self.fixture("binary-with-crlf-bytes"))
        self.assertIn(b"\r\n", tree["shared-components/static-files/x.png"])

    def test_digest_is_order_independent(self):
        self.assertEqual(tree_digest({"a": b"1", "b": b"2"}),
                         tree_digest({"b": b"2", "a": b"1"}))

    def test_digest_separates_path_from_content(self):
        # A digest concatenating path and bytes without length framing
        # collides here; this asserts the framing exists.
        self.assertNotEqual(tree_digest({"ab": b"c"}), tree_digest({"a": b"bc"}))

    def test_contains_allows_extra_paths_in_superset(self):
        # The generic subset helper allows extra paths. Import additionally
        # checks receipt tombstones; this helper alone is insufficient.
        self.assertTrue(tree_contains({"a": b"1"}, {"a": b"1", "b": b"2"}))

    def test_contains_rejects_missing_or_changed_path(self):
        self.assertFalse(tree_contains({"a": b"1"}, {}))
        self.assertFalse(tree_contains({"a": b"1"}, {"a": b"2"}))
```

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_trees.py -v`.

## Task 4: Content reconciliation

**Files:** scripts/teamlib/reconcile.py, scripts/tests/test_reconcile.py.
**Interface:** `reconcile(base: Tree, head: Tree, mine: Tree,
source_base: Tree | None = None) -> Decision`. `base` is the previous capture;
`source_base` is its reconciled result. Omission is only for initial alignment,
where the two trees are equal. Normal export passes both checkpoint trees.

- [ ] Add the regression tests below and verify module/function failure first.
- [ ] Implement the complete state table, shared by binary/text files:

```python
def reconcile(base, head, mine, source_base=None):
    if source_base is None:
        source_base = base
    absent = object()
    output, conflicts = {}, []
    for path in sorted(set(base) | set(source_base) | set(head) | set(mine)):
        b, s, h, m = (tree.get(path, absent)
                      for tree in (base, source_base, head, mine))
        if m == h:
            selected = m
        elif m == b:
            selected = h
        elif h == s == b:
            selected = m
        else:
            conflicts.append(path)
            continue
        if selected is not absent:
            output[path] = selected
    return Decision(output, tuple(conflicts))
```

```python
import unittest
from teamlib.reconcile import reconcile

class ReconcileTests(unittest.TestCase):
    def test_colleague_addition_is_preserved(self):
        d = reconcile({}, {"new": b"alice"}, {})
        self.assertEqual(d.tree, {"new": b"alice"})
        self.assertFalse(d.conflicts)
    def test_stale_existing_page_preserves_head(self):
        d = reconcile({"p": b"old"}, {"p": b"alice"}, {"p": b"old"})
        self.assertEqual(d.tree, {"p": b"alice"})
    def test_colleague_deletion_stays_deleted(self):
        self.assertEqual(reconcile({"p": b"x"}, {}, {"p": b"x"}).tree, {})
    def test_conflicting_content_is_not_applied(self):
        d = reconcile({"p": b"old"}, {"p": b"a"}, {"p": b"b"})
        self.assertEqual(d.conflicts, ("p",))
    def test_modify_delete_conflicts(self):
        self.assertEqual(reconcile({"p": b"old"}, {"p": b"a"}, {}).conflicts,
                         ("p",))

    def test_successive_builder_edit_and_revert(self):
        a, b, c = ({"p": x} for x in (b"A", b"B", b"C"))
        first = reconcile(a, a, b)
        self.assertEqual(first.tree, b)
        for mine in (c, a):
            d = reconcile(b, b, mine, source_base=first.tree)
            self.assertEqual(d.tree, mine)
            self.assertFalse(d.conflicts)

    def test_checkpoint_keeps_unimported_git_change(self):
        a, b, c = ({"p": x} for x in (b"A", b"B", b"C"))
        self.assertEqual(reconcile(a, b, a, source_base=b).tree, b)
        self.assertEqual(reconcile(a, b, c, source_base=b).conflicts, ("p",))

# Standalone receipt regression tests.
from teamlib.trees import receipt_satisfied

class ReceiptTests(unittest.TestCase):
    def test_deletion_cannot_be_resurrected(self):
        result = {"application.apx": b"app demo"}
        old = {**result, "pages/p7.apx": b"old page"}
        self.assertFalse(receipt_satisfied(result, {"pages/p7.apx"}, old))
        self.assertTrue(receipt_satisfied(result, {"pages/p7.apx"}, result))
        self.assertTrue(receipt_satisfied(result, {"pages/p7.apx"},
                                          {**result, "pages/p8.apx": b"new"}))
```

- [ ] Exhaustively enumerate one-path states missing, empty, A and B for
  base/source_base/head/mine. Check the specification table for all 256 cases;
  add multi-path cases combining a safe edit and conflict.
- [ ] Assert callers never apply Decision.tree when conflicts is nonempty.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_reconcile.py -v`.

## Task 5: Import baselines, export checkpoints, durable captures and receipts

**Files:** scripts/teamlib/state.py, scripts/teamlib/control_store.py,
scripts/sql/control_metadata.sql, scripts/tests/test_state.py,
scripts/tests/live/test_app_lock.py.
**Interfaces:** `save_capture(target, base, source_base, head, mine,
diagnostics) -> str`; `load_baseline(target) -> Baseline`;
`save_verified_baseline(target, commit, tree) -> None`; `save_checkpoint(target,
captured, reconciled, original_head, receipt_id) -> None`;
`load_checkpoint(target, head) -> Checkpoint` (including ancestry validation).

- [ ] Implement setup-state through the metadata write profile. Install exact
  versioned structures only in METADATA_SCHEMA, at the same column-level detail
  Plan 2 Task 2 gives its own tables so neither task leaves an implementer
  inventing a schema: TEAM_CONTROL_META(version, project_id),
  TEAM_APP_REGISTRY(target_key, checkout_uuid, host, registered_by_user,
  registered_at), TEAM_APP_MUTEX(target_key, owner_token, checkout_uuid, host,
  acquired_by_user, acquired_at, generation, is_uncertain), and
  TEAM_APP_TRANSFER(transfer_id, target_key, old_checkout_uuid,
  new_checkout_uuid, actor, capture_recovery_id, transferred_at) —
  append-only, since spec §9's "every transfer is recorded in metadata"
  otherwise has no table to live in. capture_recovery_id points at the
  pre-transfer capture spec §9 requires ("the capture taken beforehand") —
  without it the table records that a transfer happened but not the evidence
  a reviewer would need to check it was safe.
  TEAM_APP_REGISTRY's primary key is (target_key, checkout_uuid), not
  target_key alone: the shared application is a roster (spec §9), so more than
  one checkout legitimately registers against the same target_key at once.
  Seed a TEAM_APP_MUTEX row (owner_token NULL, generation 1, is_uncertain 0)
  for every target_key declared across the current profiles — the developer
  alias and every downstream deployment target alike — as part of this same
  operation. This is the primary seeding path and the only one downstream
  targets ever get: deploy-app acquires this mutex (Task 5's acquire_app
  bullet below) without ever running register-app, so a row that only
  register-app could create would leave every downstream target permanently
  unacquirable. Re-running setup-state after adding a new alias or target must
  seed the new row without disturbing existing ones.
  Define primary/unique keys, state/target checks and NOT NULL contracts.
  Partial or incompatible setup refuses. Plan 2 extends this shared schema; it
  does not create a second controller identity.
- [ ] register-app records a checkout against a target. For the **shared
  development application** the registry is a roster (spec §2, §9): multiple
  concurrent checkouts are expected, every registration succeeds, and
  `--transfer-from` is neither required nor accepted. Refusing a second checkout
  here would lock out the whole team after the first developer registered, so a
  test asserting that refusal would be asserting the bug. Verify registration
  before developer app mutation, and record host and user so a held mutex and a
  recovery can name a person. Idempotently insert the same TEAM_APP_MUTEX row
  setup-state seeds (owner_token NULL, generation 1, is_uncertain 0) if it is
  somehow still absent — a safety net for an alias added after the last
  setup-state run, not the primary seeding path (see setup-state above). Test
  that register-app never overwrites an existing mutex row's state — a
  developer registering against a target someone else is mid-import against
  must not reset owner_token, generation or is_uncertain.
- [ ] Keep exclusive-checkout and transfer semantics for a target declared
  single-owner — an optional isolated developer copy (spec §2.1). There a
  transfer requires the old UUID, a retained fresh capture and proof the old
  worker ended; unknown liveness blocks. Drive this from `Target.ownership_mode`
  (config.py, from `APP_OWNERSHIP_MODE`), not from the command name, so the
  shared and isolated paths cannot diverge in their guards.
- [ ] Define acquire_app(target_key, run_token, checkout_uuid, host,
  acquired_by_user) and release_app(target_key, run_token,
  confirmed_success=False) on control_store.py. `target_key` is
  `app_lock_key(target)`'s output (defined later in this task) — the
  physical identity, never the local `state_key`. Acquisition uses spec §7's
  `SELECT ... FOR UPDATE NOWAIT` transition plus its app-target addendum,
  never a bare conditional `UPDATE`: Oracle blocks a
  conditional update on the row lock until the holding transaction ends, so an
  importer that crashed before committing freezes every later importer inside
  its SQLcl subprocess with no error message. Distinguish five outcomes — one
  more than the migration mutex, because TEAM_APP_MUTEX also carries
  `is_uncertain` — and test each: success; `ORA-00054` (another transaction
  holds the row — in-flight or uncommitted crash);
  `ORA-20001 MUTEX_HELD:<token>` (cleanly held, and the token names the
  holder for the blocking message);
  `ORA-20002 TARGET_UNCERTAIN` (unheld but flagged uncertain by an interrupted
  import; acquisition must refuse until `recover-app-lock` clears it, never
  proceed past an unchecked `is_uncertain`);
  `NO_DATA_FOUND` (never bootstrapped for this target_key — a setup-required
  error meaning setup-state has not been run, or not re-run since this target
  was added; see the setup-state bullet above). Any other error is a
  hard failure. A subprocess wall-clock timeout is a backstop for network stalls
  only and is never read as "not acquired": it leaves the target uncertain and
  blocked.
  Hold this app-target mutex across import and verification; do not expire or
  steal it. Recovery follows exact-token/no-live-worker rules. Downstream deploy
  uses the same mutex without developer registration. A second concurrent
  *import* must fail to acquire the same target; a second *checkout* of the
  shared application must not.
- [ ] `mark_payload_starting(target_key, run_token)` sets `is_uncertain = 1`
  in a single `UPDATE ... WHERE target_key = :target_key AND owner_token =
  :run_token` — the held-token match makes a call from anyone who does not
  currently hold the row an error, not a silent no-op. Both import_app and
  deploy_app call it immediately before their own destructive payload runs,
  after every pre-check (registration, baseline/receipt comparison) has
already passed cleanly. Commit this transition before launching payload SQL.
  This mirrors Plan 2's "commit RUNNING before the payload": a crash gives the
  process no chance to write a failure marker
  afterward, so uncertainty must be the state the instant risk begins, not
  something a handler writes on the way out — without this step, nothing in
  either plan ever sets `is_uncertain` at all, so `TARGET_UNCERTAIN` and
  recover-app-lock's clearing of it would have nothing to do.
  `release_app` clears owner_token unconditionally (when the token matches)
  and clears `is_uncertain` plus increments generation (the bullet below) in
  the same transaction only when the caller passes `confirmed_success=True`;
  import_app and deploy_app pass it only after their entire flow — payload
  through post-write verification — has succeeded. A refusal caught before
  `mark_payload_starting` ever ran releases cleanly with `is_uncertain` still
  0, since nothing risky happened; a failure caught after it ran releases
  with `is_uncertain` still 1, correctly requiring recover-app-lock's review
  before the next attempt; a crash severe enough to skip even that release
  leaves owner_token held too, so the next acquirer sees `MUTEX_HELD` first —
  either way, nothing proceeds without review. Call release only after all
  payload workers are known to have ended; unknown worker/result state retains
  ownership. Test clean refusal, ended failure, timeout/live worker and crash.
- [ ] Maintain a monotonic import generation for each target alongside the
  mutex, incremented by `release_app`'s `confirmed_success=True` path when an
  import completes, **or by recover-app-lock when it clears a held or
  uncertain target**, so a reader can prove no import —
  successful, crashed, or crashed-and-recovered — intervened during its
  capture (spec §2.1). Without the recover-app-lock case, a crash that
  partially writes and then gets cleared entirely inside another developer's
  capture window would leave generation unchanged at both of that reader's
  observations (Task 8) — the crash never reached completion to bump it, and
  by the second read the mutex is already clear — letting a torn capture
  through undetected. Expose it through a single read-only
  `read_app_sync_state(target_key)` returning generation, current mutex
  holder and an explicit uncertain-target flag, so export never acquires
  anything. Report uncertainty as its own field rather than leaving it
  inferable only from a retained mutex: export's acceptance rule would
  otherwise depend silently on recovery discipline implemented in Task 9.
  Test that an import which begins and completes entirely between two reads
  is still detected, that a partially written import leaves the flag set,
  and that a crash-then-recover cycle entirely inside another reader's
  capture window bumps generation even though no import ever completed.
- [ ] recover-app-lock requires worker-termination evidence and retained
  current target capture in every case. `--run-token` is required only when
  the target is currently held, matched exactly against the live owner_token;
  omit it when the target is already unheld. Clear owner_token (if held) and
  `is_uncertain` together, in the same reviewed operation, always — never one
  without the other. This is what "resolving uncertain app state" means, and
  it is why `--run-token` cannot be unconditionally required: a target left
  unheld-but-uncertain — the routine result of `release_app`'s
  `confirmed_success=False` path after a cleanly caught import failure, not
  only a rare crash — would otherwise have no token for a second recovery to
  match, and no other command ever clears `is_uncertain` —
  permanently locking the target out of every future acquisition. It cannot
  stamp a verified baseline from a lock-clear operation — that still requires
  a subsequent successful import or adopt-app. Refuse production and
  unverifiable worker state.
  Read the recovery-owner role for this target from its tracked
  `targets/*.json` contract and include it, per spec §7's self-explaining-
  blocking requirement, in every refusal caused by a held app-target mutex.
  There is no membership check on who invokes the command — an org role is not
  a database identity — so printing the name is what lets a human enforce it.
- [ ] Implement `app_lock_key(target)` exactly as spec §5's physical identity
  tuple. Registry, generation and mutex use only that key. Local state uses a
separate `state_key` over full Target identity, including binding digest. Load
  tracked `targets/controllers.json`, keyed by verified instance_id, naming
  exactly one METADATA owner per instance. Setup and every app mutation verify
  the selected controller against this contract; `.env` cannot override it. A
  controller move requires coordinated migration of registry/lock state, never
  bootstrap of a parallel empty store. Test two connection names, service
  aliases, roles and binding digests targeting one app: identical mutex key,
  different local state keys, one import winner. Different
  instance/workspace/app IDs produce distinct mutex keys.
- [ ] Store versioned manifests, immutable blobs and operation records under
  .sync-state. Binding changes invalidate local baseline/checkpoint/receipt
  reuse, never physical lock identity. Retain blobs independently of Git.
- [ ] Journal the captured/reconciled checkpoint pair with the source patch and
  receipt. Advance only after complete result verification, including no-op
  exports and explicit resolution; initialize both on verified adoption/import.
  Follow spec §6's exact-result commit anchoring before reuse. Missing ancestry
  refuses with retained evidence instead of selecting a stale baseline. Failed
  patch, uncommitted result and branch rewind tests must not advance/reuse it.
  Checkpoints are GC roots; checkpoint ancestry refs must remain reachable or
  operations refuse. An import baseline never advances merely on export.
- [ ] Store capture receipts as a path/SHA-256 manifest plus its referenced
  content-addressed blobs — the same on-disk shape baselines already use
  (spec §6) for the same reason: deduplicated storage, not a bare digest a
  later comparison has nothing to compare against. This manifest+blobs pair
  is resolved into an ordinary `Tree` (Task 3, `dict[str, bytes]`) by reading
  the referenced blobs before it reaches `tree_contains` or any other
  Tree-typed function; the stored shape is a storage optimization, never a
  second type those functions need to accept. "Successfully accounted for,"
  not "successfully written": a zero-change export (Task 8) reconciles
nothing to disk but still receipts the manifest it confirmed. Resolution
  receipts additionally record conflict paths and resolved digest. These are not
  database-alignment baselines. Also persist the sorted required-absence set
  from spec §6 with every receipt. Tombstones survive no-op captures; only a
  reconciled re-add removes one. Test deleted-page resurrection, older commits,
  legitimate new paths, binary/empty paths and corrupted absence evidence.
- [ ] Atomic-save manifests only after all blobs exist; validate hashes on load.
  An existing baseline becomes uncertain at the start of an import mutation.
- [ ] Add tests for target rebinding, pruned commit with retained blobs,
  corrupted manifests/blobs, partial save, concurrent local operation refusal
  and receipts that do not match the chosen commit.
- [ ] Recovery captures survive every failure and process restart. Garbage
  collection is an explicit command listing exact recoverable entries; never
  delete captures as an EXIT side effect. Baselines, receipts, quarantined state
  and interrupted journals are GC roots; shared blobs cannot be removed while
  referenced. Corrupt/unreadable roots block collection. Test these cases.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_state.py -v`.

## Task 6: Journaled file-level application

**Files:** scripts/teamlib/patch.py, scripts/tests/test_patch.py.
**Interface:** `apply_tree(repo, alias, expected_head, before, after,
recovery_id) -> None`; `recover_files(operation_id, action) -> None`.

- [ ] Refuse conflicts, changed HEAD, dirty source and mismatched before
  manifest. Acquire an app-local lock and recheck immediately before writing.
- [ ] Compute writes/deletes only over owned paths, persisting every preimage
  and postimage in the journal before the first write.
- [ ] Replace individual files through sibling temporary files and os.replace;
  remove only explicit files whose current hashes match expected preimages.
  Never recursively delete apps/<alias> or deployments.
- [ ] Verify full result, then record completion and receipt. On failure leave
  journal/recovery intact and block further app operations.
- [ ] Inject failure after each write/delete, test finish/restore and refuse
  recovery over a later user edit. Assert all deployment bindings and unrelated
  files remain byte-identical.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_patch.py -v`.

## Task 7: Master contracts and tool qualification

**Files:** targets/masters.json, scripts/teamlib/masters.py,
scripts/tests/test_masters.py, docs/toolchain.md.
**Interface:** `validate_masters(source_tree, target, contract) -> MasterReport`.

- [ ] Parse subscription/master properties from qualified APEXlang fixtures;
  ignore quoted SQL, comments and non-master @ references. Reject unsupported
  subscription syntax explicitly.
- [ ] Contract entries contain app ID, app alias, workspace identity,
  component type and symbol. Query through the exact selected target and
  verify each required component, including built-in theme masters.
- [ ] Treat visibility failure as unknown/refusal. Do not accept ID-only
  existence as successful resolution.
- [ ] Record qualified SQLcl/JDK/APEX versions and command success/error formats.
  Cross-instance linkage is an acceptance test, not inferred from the earlier
  same-instance spike.
- [ ] Tests: correct app/wrong component, reused ID, invisible master,
  malformed export, built-in theme and valid external master.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_masters.py -v`.

## Task 8: Export and explicit bootstrap

**Files:** scripts/teamlib/apex.py, scripts/tests/test_export_app.py.
**Interfaces:** `capture_app(target, held_by=None) -> Capture` (Capture has
tree, recovery_id, target and the sync state observed either side of the
read); `export_app(target) -> Decision`. `held_by` is the general rule for
any caller that already holds the app-target mutex when it captures, not a
special case for one specific call site — see the bracket bullet below.

- [ ] Implement spec §6 export sequence using Tasks 2–7. Capture first, retain
  before mutation, reconcile contents, journal the patch, verify and receipt.
- [ ] Bracket the capture with `read_app_sync_state` (Task 5). Refuse before
  starting if the target is uncertain or an import holds the mutex — **unless
  the caller passes `held_by=` its own held run_token**, in which case a
  holder matching that token is expected, not a competing import, and does
  not refuse. The rule is
  general, not tied to one call site: **any** operation that already holds
  the app-target mutex when it captures must pass its own token — this is
  every internal capture inside import_app (Task 9's pre-replacement check,
  its pre-destructive-step recheck, and its post-import re-export alike) and
  inside deploy_app (Plan 3 Task 4's destination capture and its own
  post-deployment re-export), because all of them run after their caller has
  already acquired the same mutex. A caller with no held mutex of its own —
  an ordinary developer export — passes none and refuses on any holder at
  all. After the read, accept the capture only if the generation is unchanged
  and no *other* holder appeared AND neither observation is uncertain. For
  the importing/deploying worker's internal captures only, uncertainty is
  permitted while the same `held_by` token remains the owner at both reads;
  ordinary exports cannot use this exception. Otherwise discard as torn and
  retry a bounded number of times before reporting that an import is in
  progress. **Export never acquires the mutex** — readers that lock can
  block the team and strand a lock when a developer's export dies, and two
  concurrent exports are harmless (spec §2.1). A discarded capture has no
  side effects, so retry is free. Without the `held_by` carve-out, every one
  of import_app's and deploy_app's own internal captures deadlocks against
  the lock its caller is itself holding — test each of those call sites
  explicitly, not just one representative case.
- [ ] Recovery uses a separate evidence-only capture path after establishing
  worker termination, guarded by a controller recovery claim on the same
  physical target row. It accepts held/uncertain state only for that selected
  recovery and never feeds reconciliation, receipts or baseline stamping.
  Claim by a NOWAIT compare-and-set of the selected owner (including NULL)
  and generation to a new recovery token after worker-termination proof; keep
  uncertainty set. Concurrent normal acquisition must see the recovery holder.
  Retain the claim through capture and atomic clear/generation increment;
  concurrent recoveries cannot clear a different worker's state.
- [ ] Test a caught partial import that clears ownership but leaves generation
  unchanged and uncertainty set, both before export and inside its read window.
  Neither capture may be reconciled. Test successful owner's post-import
  capture, evidence-only recovery, wrong-token refusal and concurrent recovery.
- [ ] Record a capture receipt even when reconciliation changes zero paths
  (spec §6). Test that a no-op export still receipts and that a subsequent
  import of the matching commit is allowed by it. This is the ordinary
  pre-import export when nobody has uncaptured work; without the receipt the
  refusal it is meant to clear becomes an unbreakable loop.
- [ ] bootstrap-app requires absent tracked alias source, captures the existing
  app and writes a reviewable candidate without database writes. adopt-app
  requires clean committed source and exact re-export equality before baseline.
- [ ] Reject export for integration/test/replay regardless of binding filename.
  Verify actual workspace/app identity and alias match.
- [ ] Test through the public CLI in temporary Git repositories with a fake
  SQLcl process: colleague changes, both changes, missing baseline, false-success
  partial export, dirty source, source directory symlink, rebinding, preserved
  deployments and HEAD movement. Assert actual filesystem and exit status.
- [ ] Test the torn-capture cases with a fake SQLcl that mutates the sync state
  mid-read: an import already holding the mutex when export starts, an import
  starting and still running when the capture ends, an import that begins and
  completes entirely within the capture window, and an import that begins,
  partially writes, crashes and is cleared by recover-app-lock entirely
  within the capture window (Task 5). All four must discard the capture and
  write no tracked source. Add the negative: two concurrent exports must both
  succeed and neither may block the other.
- [ ] Verify a refusal prints the durable capture location and resolution
  command, never an unconditional instruction to import over Builder work.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_export_app.py -v`.

## Task 9: Guarded import with capture protection

**Files:** scripts/teamlib/apex.py, scripts/tests/test_import_app.py.
**Interface:** `import_app(target, commit, replace_from=None) -> Baseline`.

- [ ] Verify checkout registration and acquire the shared app-target mutex
  through control_store before capture. Refuse parallel clients and retain the
  lock on unknown import state until reviewed recovery; release with
  `confirmed_success=True` on verified success, or release plainly (leaving
  `is_uncertain` at whatever `mark_payload_starting` left it, if it ran at
  all) on a known refusal before payload starts.
- [ ] Resolve the commit, require clean source, materialize exact owned blobs
  and validated binding into scratch.
- [ ] Capture current app before replacement. Require baseline equality, a
  capture receipt satisfied by selected source via `receipt_satisfied` (Task 3),
  a resolution receipt binding the exact capture and selected source digest, or
  the exact --replace-from capture for first alignment. Extra selected paths
  are allowed only when they do not resurrect required-absent paths. Verify
  absent-app bootstrap explicitly; no generic --force flag.

- [ ] Treat the baseline-mismatch refusal as the design's primary guard, not a
  conservative default (spec §6). The application is shared, so the work it
  protects belongs to colleagues who do not know the command is running and
  cannot consent to losing it. The refusal names the paths that differ and the
  export that would preserve them; no flag waives it. It states both
  possibilities and the single action that resolves either — run `export-app`;
  if it reports changes, review and commit them before retrying; if it reports
  none, retry immediately. Do not assert that a colleague has uncaptured work:
  at refusal time that has not been established, and sending a developer to
  interrupt a colleague when the answer is "you have not exported yet" is how a
  correct guard acquires a reputation for crying wolf.
- [ ] Increment the target's import generation on completion (Task 5) so a
  concurrent export can prove whether its capture straddled this import.
  Increment on the completion path only — a refused or failed import that wrote
  nothing must not advance it, and one that wrote partially must leave the
  target uncertain rather than merely bumping a counter.
- [ ] Print the team-pause requirement before any write: this import overwrites
  the application everyone is editing, and §9 makes announcing it part of the
  operation. State the alias, target identity and expected duration. This is the
  floor. Plan 3 Task 3 adds `announce-import`, which drafts the message from
  observed state and confirms with the developer; keep this print correct on its
  own, because Plan 1 must be usable before Plan 3 exists.
- [ ] Validate masters/source, guard write, recheck capture and target, call
  `mark_payload_starting(target_key, run_token)` (Task 5), import, then call
  `capture_app(target, held_by=run_token)` to re-export and verify bytes plus
  master linkage without tripping its own held-mutex refusal. Baseline
  becomes verified, and `release_app` runs with `confirmed_success=True`,
  only after every check passes. A known failure with all workers ended
  releases without confirmed_success and leaves uncertainty set. Unknown
  worker/result state retains ownership and uncertainty until reviewed recovery.
- [ ] Test uncaptured Builder edit refusal, captured-and-committed edits,
  dirty source, stale receipt, changed app between captures, import error-zero,
  subscription mismatch, interrupted verification and successful stamping.
  Include a second developer's uncaptured Builder work as the refusal case, and
  assert the generation does not advance on any refused or failed path.
- [ ] Demonstrate that every DB write occurs after exact in-session identity
  assertion and no failure path reports a current baseline.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_import_app.py -v`.

## Task 10: Resolution flow and command integration

**Files:** scripts/team.py, scripts/teamlib/state.py,
scripts/tests/test_recovery_flow.py.
**Interface:** `resolve_export(recovery_id: str, resolved: Path) -> Decision`.

- [ ] Wire all documented commands with argparse; no implicit commit or push.
- [ ] resolve-export takes a validated complete resolved source tree and the
  immutable recovery ID. Require original HEAD/preimage unchanged, or re-run
  reconciliation against new HEAD and retain a new bundle.
- [ ] Use Task 6 for writes, record the resolution receipt, and print review,
  commit and import steps. Reject unresolved conflict marker content where
  produced by tooling, missing paths and extra unsafe files.
- [ ] Test the full Bob scenario: capture conflict, restart process, resolve,
  user commit, import, then verified re-export. Also add a new Builder edit
  after capture and prove import refuses without losing either version.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_recovery_flow.py -v`.

## Task 11: Thin Bash/PowerShell launchers and native parity

**Files:** scripts/team.sh/.ps1, scripts/export_app.sh/.ps1,
scripts/import_app.sh/.ps1, scripts/tests/test_launchers.py,
.github/workflows/template-checks.yml.

- [ ] Launch Python with quoted argv and propagate its status; choose python3
  or py -3 only after a version check. Do not duplicate config or reconciliation.
- [ ] PowerShell dynamic environment reads use the .NET API. Pass arrays,
  never an interpolated command string; test native Windows SQLcl launch.
- [ ] Run the same parameterized public-command fixtures on Linux Bash,
  Windows Git Bash, PowerShell 7 and Windows PowerShell 5.1.
- [ ] Add CI syntax checks, full Python tests, case-collision and LF checks.
  Execute Bash syntax validation once per file, not bash -n scripts/*.sh
  (which does not parse every positional argument as a script).

```bash
for script in scripts/*.sh; do
  bash -n "$script" || exit
done
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v
```

## Task 12: Live acceptance, documentation and handoff

**Files:** scripts/tests/live/test_apex_round_trip.py, docs/toolchain.md,
docs/app-recovery.md.

- [ ] Use explicitly provisioned disposable APEX targets with exact profiles;
  run all write guards. Never default to the historical docker-demo fixture.
- [ ] Verify determinism, round-trip, binaries, subscription linkage,
  target remapping, missing/wrong master and full capture/resolve/import flow.
  Include cross-instance masters, not just two apps on one instance, and payload
  attempts to mutate isolated metadata.
- [ ] Verify the shared-application concurrency cases live, against a real
  application two checkouts both target (spec §2): two checkouts registering
  must both succeed; two concurrent exports must both succeed; a second
  concurrent import must fail to acquire the mutex; and an export whose capture
  straddles a real import must discard rather than commit a torn tree. The
  refusal expected here is the second *import* and the torn *capture* — never
  the second checkout, which is the ordinary case.
- [ ] Capture tool versions and signed-off test evidence; no automatic Git
  staging/commit in test code. Do not classify unrun live cases as passing.
- [ ] Run all offline/native suites once more and review diffs. Document
  remaining Builder race and import-failure boundaries.
- [ ] Keep live cases available to Plan 3's required isolated CI gate.

## Completion checklist

- [ ] Pure reconciliation exhaustive cases and real wrapper filesystem cases pass.
- [ ] Baseline provenance, receipts and interrupted recovery are tested.
- [ ] Concurrent checkouts and concurrent exports of the shared application both
  succeed; concurrent imports and torn captures are refused.
- [ ] Target/deployment agreement and production write refusal are tested.
- [ ] All four shell/platform surfaces execute the shared core successfully.
- [ ] Live qualification evidence exists, including cross-instance subscriptions.
- [ ] Plan 2 consumes Target/run_sqlcl without another safety implementation.

No task is complete merely because an argument-validation test passes.
