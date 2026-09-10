# Review Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Checkboxes are implementation work, not verification already performed.

**Goal:** Close all 27 findings from the 2026-09-09 code review without changing
the safety architecture that the review found sound.

**Architecture:** Five phases ordered by blast radius. Phase 1 restores command
paths that crash or silently misbehave today. Phase 2 replaces two hand-written
SQL lexers with one Oracle-aware masker in a new shared module, which fixes both
the q-quote parsing defect and the cross-module private-import smell at once.
Phase 3 fixes the two defects that fail on scale rather than syntax. Phase 4
repairs CI and declares dependencies. Phase 5 is operational hardening. Every
behavioural change keeps the existing fail-closed semantics: guards become more
precise, never more permissive, except where a guard currently rejects valid
Oracle SQL.

**Tech Stack:** Python 3.10+ (stdlib only for the core workflow), `unittest`,
qualified SQLcl/Oracle, thin Bash/PowerShell launchers, GitHub Actions.

**Spec:** [Team design](../specs/2026-09-06-team-template-design.md) — the
safety contract this plan must not weaken. Findings and reproduction evidence:
the published review at
`https://claude.ai/code/artifact/cb3aee5b-e2ee-4154-8830-f782c69518ef`.

---

## Global Constraints

Copied verbatim from the existing plans and `AGENTS.md`; every task inherits them.

- **Python 3.10+.** No third-party imports in the core team workflow. New
  third-party dependencies are permitted only in `runbook.py` (`cryptography`,
  already used) and the graphify tooling.
- **LF line endings** for `*.apx`, `*.sql`, `*.json`, `*.sh`, `*.ps1` (`.gitattributes`).
  Every file written by Python uses `newline="\n"` or `newline=""`.
- **Production writes are refused.** No task may add a code path that writes to a
  production-classified target. SELECT-only status stays allowed through a
  verified read profile.
- **Metadata writes always use the METADATA profile,** isolated from tables/code
  payload users.
- **No one-off shell bypass, no second reconciliation implementation, no
  database-as-source shortcut** (`self_improve.md`).
- **A focused regression test comes before the implementation** (`self_improve.md`).
  Every task below is written in that order.
- **No command commits or pushes automatically.**
- **Commit message style matches the repository:** an imperative capitalised
  sentence, no `feat:`/`fix:` prefix (see `git log`). End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  ```

**The verification command for every task:**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v
```

Baseline before starting: **169 tests, OK, ~2.3 s.** Any task that reduces the
passing count has regressed something.

**Test file preamble.** Every new test file in `scripts/tests/` starts with the
established preamble, copied exactly:

```python
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
```

---

# Phase 1 — Restore the broken command paths

## Task 1: Fix the `apply-release` crash

`release_adapter.main()` raises `NameError: name 'os' is not defined` whenever
`--env` is omitted, because `os` is used at line 207 but never imported. The
`or` chain short-circuits when `--env` is present, which is why CI never hit it
and why no test caught it: `test_release_adapter.py` only calls
`apply_verified_release()` directly and never `main()`.

**Files:**
- Modify: `scripts/teamlib/release_adapter.py:5-11` (imports)
- Test: `scripts/tests/test_release_adapter.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `teamlib.release_adapter.main(argv: list[str] | None = None) -> int`
  becomes callable without `--env`. Task 2 depends on this.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_release_adapter.py`, inside `ReleaseAdapterTests`:

```python
    def test_main_resolves_environment_without_an_explicit_env_flag(self):
        with tempfile.TemporaryDirectory(prefix="team-release-adapter-cli-") as directory:
            root = Path(directory)
            (root / "dummy.json").write_text("{}", encoding="utf-8")
            (root / "dummy.tar").write_bytes(b"")
            argv = [
                "apply-release",
                str(root / "dummy.tar"),
                "--plan", str(root / "dummy.json"),
                "--history", str(root / "dummy.json"),
                "--target", str(ROOT / "targets" / "test.json"),
            ]
            # The environment profile is absent, so main() must fail with the
            # diagnostic SystemExit -- not with NameError from a missing import.
            with self.assertRaises(SystemExit) as caught:
                main(argv)
            self.assertIn("environment profile file not found", str(caught.exception))
```

Extend the existing import line in that file:

```python
from teamlib.release_adapter import ReleaseAdapterError, apply_verified_release, main
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_release_adapter -v 2>&1 | tail -20
```

Expected: `NameError: name 'os' is not defined`.

- [ ] **Step 3: Add the missing import**

In `scripts/teamlib/release_adapter.py`, the import block becomes:

```python
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_release_adapter -v
```

Expected: OK.

- [ ] **Step 5: Prove no other module has this defect**

Run this repo-wide runtime undefined-name sweep and record the output in the
commit message:

```bash
python3 - <<'PY'
import ast, builtins, pathlib
BUILTINS=set(dir(builtins))
issues=[]
for p in sorted(pathlib.Path('scripts').rglob('*.py')):
    tree=ast.parse(p.read_text(encoding='utf-8'), filename=str(p))
    bound=set(BUILTINS)|{'__file__','__name__'}
    for c in ast.walk(tree):
        if isinstance(c,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): bound.add(c.name)
        if isinstance(c,(ast.Import,ast.ImportFrom)):
            for a in c.names: bound.add((a.asname or a.name).split('.')[0])
        if isinstance(c,ast.Name) and isinstance(c.ctx,(ast.Store,ast.Del)): bound.add(c.id)
        if isinstance(c,ast.arg): bound.add(c.arg)
        if isinstance(c,ast.ExceptHandler) and c.name: bound.add(c.name)
        if isinstance(c,(ast.Global,ast.Nonlocal)): bound.update(c.names)
    for c in ast.walk(tree):
        ann=None
        if isinstance(c,ast.AnnAssign): ann=c.annotation
        elif isinstance(c,ast.arg): ann=c.annotation
        elif isinstance(c,(ast.FunctionDef,ast.AsyncFunctionDef)): ann=c.returns
        if ann is not None:
            for s in ast.walk(ann):
                if isinstance(s,ast.Name): s._ann=True
    for c in ast.walk(tree):
        if isinstance(c,ast.Name) and isinstance(c.ctx,ast.Load) and not getattr(c,'_ann',False):
            if c.id not in bound: issues.append(f"{p}:{c.lineno}: {c.id}")
print("\n".join(issues) or "no runtime undefined names")
PY
```

Expected: `no runtime undefined names`.

- [ ] **Step 6: Commit**

```bash
git add scripts/teamlib/release_adapter.py scripts/tests/test_release_adapter.py
git commit -m "$(cat <<'EOF'
Import os in the release adapter CLI entry point

apply-release raised NameError whenever --env was omitted, because the
TEAM_ENV_FILE/PROJECT_ENV_FILE fallback used os without importing it. The
or-chain short-circuited when --env was present, so CI never reached it and
no test called main(). A repo-wide runtime undefined-name sweep confirms this
was the only occurrence.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Stop discarding a global `--env` on offline commands

`team.py` advertises `--env` on the top-level parser and lifts it into
`args.env_file`, but `_offline()` builds the handler argv as
`[args.command, *args.args]` and never forwards it. So
`team.py --env prod.env apply-release …` silently falls back to `.env`.
Only two offline handlers accept `--env`: `apply-release` in both
`teamlib/release.py:545` and `teamlib/release_adapter.py`.

**Files:**
- Modify: `scripts/team.py:511-540` (`_offline`)
- Test: `scripts/tests/test_launchers.py`

**Interfaces:**
- Consumes: `teamlib.release_adapter.main` from Task 1.
- Produces: module-level `ENV_AWARE_OFFLINE_COMMANDS: frozenset[str]` in
  `scripts/team.py`. No later task depends on it.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_launchers.py`:

```python
class OfflineEnvForwardingTests(unittest.TestCase):
    def test_global_env_flag_reaches_an_env_aware_offline_handler(self):
        captured: list[list[str]] = []

        def spy(argv=None):
            captured.append(list(argv or []))
            return 0

        with patch("teamlib.release_adapter.main", spy):
            team.main([
                "--env", "profiles/test.env", "apply-release", "release.tar",
                "--plan", "plan.json", "--history", "history.json", "--target", "t.json",
            ])
        self.assertEqual(len(captured), 1)
        self.assertIn("--env", captured[0])
        self.assertEqual(captured[0][captured[0].index("--env") + 1], "profiles/test.env")

    def test_explicit_env_after_the_subcommand_is_not_duplicated(self):
        captured: list[list[str]] = []

        def spy(argv=None):
            captured.append(list(argv or []))
            return 0

        with patch("teamlib.release_adapter.main", spy):
            team.main([
                "--env", "profiles/global.env", "apply-release", "release.tar",
                "--plan", "plan.json", "--history", "history.json", "--target", "t.json",
                "--env", "profiles/explicit.env",
            ])
        self.assertEqual(captured[0].count("--env"), 1)
        self.assertEqual(captured[0][captured[0].index("--env") + 1], "profiles/explicit.env")

    def test_global_env_is_not_forwarded_to_handlers_that_reject_it(self):
        captured: list[list[str]] = []

        def spy(argv=None):
            captured.append(list(argv or []))
            return 0

        with patch("teamlib.ci.main", spy):
            team.main(["--env", "profiles/test.env", "ci-doctor", "--contract", "ci/runner-contract.json"])
        self.assertNotIn("--env", captured[0])
```

Add to that file's imports (keep the existing preamble):

```python
from unittest.mock import patch

import team
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_launchers -v 2>&1 | tail -20
```

Expected: `AssertionError: '--env' not found in [...]` for the first test.

- [ ] **Step 3: Forward the flag to handlers that accept it**

In `scripts/team.py`, add above `_offline`:

```python
# Offline commands take explicit local inputs, but apply-release binds a
# non-production target and therefore accepts an environment profile. A global
# --env must reach it rather than being silently dropped.
ENV_AWARE_OFFLINE_COMMANDS = frozenset({"apply-release"})
```

Then replace the `handler_args` line in `_offline` with:

```python
    command_prefixed = {"new-migration", "add-dependency", "build-release", "verify-release", "plan-release", "apply-release", "adopt-baseline", "ci-doctor", "ci-replay"}
    handler_args = [args.command, *args.args] if args.command in command_prefixed else list(args.args)
    env_file = getattr(args, "env_file", None)
    if env_file and args.command in ENV_AWARE_OFFLINE_COMMANDS and "--env" not in handler_args:
        handler_args.extend(["--env", env_file])
    result = handler(handler_args)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_launchers -v
```

Expected: OK, three new tests passing.

- [ ] **Step 5: Run the full suite**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK, 173+ tests.

- [ ] **Step 6: Commit**

```bash
git add scripts/team.py scripts/tests/test_launchers.py
git commit -m "$(cat <<'EOF'
Forward a global --env to env-aware offline commands

team.py captured --env before the subcommand and then dropped it when
dispatching offline handlers, so `team.py --env prod.env apply-release`
silently fell back to .env. Forward it only to handlers that accept it, and
let an explicit --env after the subcommand win.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add a Python linter to CI and fix everything it reports

Eighteen of the twenty-seven findings are what a linter reports on a clean run,
including the Task 1 crash. Adding the gate is what stops this class of drift
returning.

**Files:**
- Modify: `.github/workflows/template-checks.yml`
- Create: `pyproject.toml` (ruff configuration only; Task 12 adds the dependency
  metadata to the same file)
- Modify: `scripts/team.py` (add `Any` import), `scripts/teamlib/apex.py`,
  `ci.py`, `control_store.py`, `deploy.py`, `patch.py`, `release.py`,
  `conflict_assistant.py`, `authoring.py`, `live_inventory.py`, `reconcile.py`,
  `migrate.py`, `migration_store.py`, `replay.py`, `migration_plan.py`
- Test: `scripts/tests/test_docs.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a `pyproject.toml` with a `[tool.ruff]` table. Task 12 extends the
  same file with `[project]`.

- [ ] **Step 1: Create the ruff configuration**

Create `pyproject.toml`:

```toml
[tool.ruff]
target-version = "py310"
line-length = 200
src = ["scripts"]

[tool.ruff.lint]
# F  pyflakes      - undefined names, unused imports (the Task 1 crash class)
# E9 syntax errors
# B  bugbear       - mutable defaults, silent except patterns
# UP unpyupgrade   - keep the 3.10+ idioms consistent
select = ["F", "E9", "B", "UP"]

[tool.ruff.lint.per-file-ignores]
# Tests deliberately import symbols to assert they are re-exported.
"scripts/tests/*" = ["F401"]
```

- [ ] **Step 2: Run ruff to see the failures**

```bash
python3 -m ruff check scripts/ 2>/dev/null || pipx run ruff check scripts/
```

Expected: 16 `F401` unused-import errors, one `F821` undefined name (`Any` in
`scripts/team.py:191`), and one `F811`/`F401` for the redundant `import io` in
`release.py:251`.

- [ ] **Step 3: Apply the mechanical fixes**

```bash
python3 -m ruff check --fix scripts/ 2>/dev/null || pipx run ruff check --fix scripts/
```

Then fix the two ruff cannot resolve on its own.

In `scripts/team.py`, add to the imports:

```python
from typing import Any
```

In `scripts/teamlib/migration_store.py`, add `import hashlib` to the top-level
imports and replace both occurrences of the string-literal import:

```python
        evidence = hashlib.sha256(f"initial:{digest}".encode("ascii")).hexdigest()
```

In `scripts/teamlib/replay.py:39`, replace `__import__("hashlib")` with
`hashlib` and add `import hashlib` at the top.

In `scripts/teamlib/migration_plan.py:106`, replace
`__import__("pathlib").Path` with `Path` and add `from pathlib import Path` at
the top.

Leave `scripts/team.py:531` alone — `__import__(f"teamlib.{module_name}", …)`
is legitimate dynamic dispatch on a validated command name.

- [ ] **Step 4: Add the lint gate to CI**

In `.github/workflows/template-checks.yml`, insert a step after the checkout and
before "Shell syntax":

```yaml
      - name: Python lint
        run: |
          python3 -m pip install --quiet ruff
          python3 -m ruff check scripts/
```

- [ ] **Step 5: Write the guard test**

Append to `scripts/tests/test_docs.py`, inside `DocumentationTests`:

```python
    def test_lint_gate_is_configured_and_wired_into_ci(self):
        config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[tool.ruff]", config)
        self.assertIn('"F"', config)
        workflow = (ROOT / ".github/workflows/template-checks.yml").read_text(encoding="utf-8")
        self.assertIn("ruff check scripts/", workflow)

    def test_no_string_literal_imports_outside_the_command_dispatcher(self):
        offenders = []
        for path in sorted((ROOT / "scripts").rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "__import__(" in line and "teamlib." not in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [], f"use ordinary imports: {offenders}")
```

- [ ] **Step 6: Verify**

```bash
python3 -m ruff check scripts/ && PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: ruff reports `All checks passed!`, suite OK.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .github/workflows/template-checks.yml scripts/
git commit -m "$(cat <<'EOF'
Add a ruff gate and clear the dead imports it reports

CI checked shell syntax, unittest, JSON and CRLF but nothing about Python
quality, which is how a missing os import shipped through a green suite.
Adds ruff (F/E9/B/UP) to the offline job, removes sixteen dead imports, adds
the missing typing.Any, and replaces four string-literal __import__ calls with
ordinary imports. The dynamic dispatch in team.py is deliberately kept.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Remove the dead code a linter cannot see

`conflict_assistant_cli.py` is a second, unreachable adapter for
`explain-conflict`, which routes to `conflict_assistant.main`. This is the
"second implementation" `self_improve.md` forbids. Three smaller items go with it.

**Files:**
- Delete: `scripts/teamlib/conflict_assistant_cli.py`
- Modify: `scripts/teamlib/migration_store.py:62-72` (`_b64_sql`),
  `scripts/teamlib/trees.py:110`, `scripts/teamlib/trees.py:230`
- Test: `scripts/tests/test_conflict_assistant.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_b64_sql(column: str) -> str` loses its `clob` keyword parameter.
  Task 5 moves this function, so apply this change first.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_conflict_assistant.py`:

```python
class SingleAdapterTests(unittest.TestCase):
    def test_explain_conflict_has_exactly_one_cli_adapter(self):
        root = Path(__file__).resolve().parents[2]
        self.assertFalse(
            (root / "scripts/teamlib/conflict_assistant_cli.py").exists(),
            "conflict_assistant_cli.py duplicates conflict_assistant.main",
        )
        from teamlib import conflict_assistant
        self.assertTrue(callable(getattr(conflict_assistant, "main", None)))

    def test_b64_sql_has_no_silently_truncating_clob_branch(self):
        import inspect
        from teamlib.migration_store import _b64_sql
        self.assertNotIn("clob", inspect.signature(_b64_sql).parameters)
        self.assertNotIn("900", inspect.getsource(_b64_sql))
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_conflict_assistant -v 2>&1 | tail -20
```

Expected: `AssertionError: True is not false : conflict_assistant_cli.py duplicates …`

- [ ] **Step 3: Delete the orphan and the dead parameter**

```bash
git rm scripts/teamlib/conflict_assistant_cli.py
```

In `scripts/teamlib/migration_store.py`, replace `_b64_sql` with:

```python
def _b64_sql(column: str) -> str:
    """Base64-encode a column so wrapped output stays safely re-joinable.

    CLOB columns are never read through this helper: they are read in bounded
    900-character chunks with an explicit part/total, so a truncating branch
    here would only ever be a silent-data-loss trap.
    """
    return (
        "UTL_RAW.CAST_TO_VARCHAR2(UTL_ENCODE.BASE64_ENCODE("
        f"UTL_RAW.CAST_TO_RAW(NVL({column}, CHR(1)))))"
    )
```

In `scripts/teamlib/trees.py`, delete the no-op ternary at line 110. The loop
body begins:

```python
        for entry in entries:
            rel_path = Path(entry.path).relative_to(root).as_posix()
            rel_path = _validate_relative_path(rel_path)
```

In `scripts/teamlib/trees.py`, replace the unreachable arrow branch in
`assert_source_clean` with a comment that records why it is not needed:

```python
        # --porcelain=v1 -z emits a rename as two NUL-separated records (new
        # path, then original) rather than an "orig -> new" pair, so there is no
        # arrow to split. The bare original-path record cannot start with the
        # alias prefix after the two-character status slice, and any record that
        # does is refused below by the non-clean status check.
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A scripts/
git commit -m "$(cat <<'EOF'
Remove the duplicate conflict CLI and three dead branches

explain-conflict routes to conflict_assistant.main, so conflict_assistant_cli
was an unreachable second adapter for one command. Also drops the never-called
clob branch of _b64_sql, whose 900-character DBMS_LOB.SUBSTR would have
silently truncated a future caller, a no-op ternary in trees, and the
unreachable arrow-splitting branch in assert_source_clean.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

# Phase 2 — SQL text correctness

## Task 5: Create the shared SQL-text module with an Oracle-aware masker

Two hand-written lexers — `migration_bundle._mask_code` and
`sqlcl._mask_sql_comments_and_literals` — both understand `'…'` but not Oracle's
alternative quoting (`q'[…]'`). The consequences run in opposite directions: the
bundle validator refuses valid SQL with "unterminated SQL comment or literal",
while the production read-only guard is fooled into flagging keywords it should
have masked. This task builds one correct masker; Task 6 rewires both callers.

The same module absorbs the SQL helpers that `control_store` currently reaches
into `migration_store` for by their underscore names.

**Files:**
- Create: `scripts/teamlib/sql_text.py`
- Test: `scripts/tests/test_sql_text.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces, all public:
  - `mask_sql(text: str) -> tuple[str, bool]` — returns masked text (same length,
    newlines preserved) and whether every construct was terminated.
  - `sql_literal(value: str) -> str`
  - `clob_builder(variable: str, value: str, *, indent: str = "  ") -> str`
  - `b64_sql(column: str) -> str`
  - `row_lines(stdout: str, prefix: str) -> list[list[str]]`
  - `SqlTextError(ValueError)`
  Tasks 6, 7, 8 and 9 all consume these exact names.

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_sql_text.py`:

```python
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import re
import unittest

from teamlib.sql_text import SqlTextError, clob_builder, mask_sql, sql_literal


MUTATION = re.compile(r"\b(?:INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE)\b", re.IGNORECASE)


class MaskSqlTests(unittest.TestCase):
    def test_q_quote_with_embedded_apostrophe_is_terminated_and_fully_masked(self):
        for text in (
            "UPDATE t SET m = q'[don't touch]' WHERE id=1;",
            "UPDATE t SET m = q'{it's a test}' WHERE id=1;",
            "UPDATE t SET m = q'(it's fine)' WHERE id=1;",
            "UPDATE t SET m = q'<it's fine>' WHERE id=1;",
            "UPDATE t SET m = q'!it's fine!' WHERE id=1;",
            "UPDATE t SET m = nq'[naive's]' WHERE id=1;",
        ):
            masked, terminated = mask_sql(text)
            self.assertTrue(terminated, text)
            self.assertEqual(len(masked), len(text), text)
            self.assertNotIn("touch", masked)
            self.assertNotIn("test", masked)

    def test_keywords_inside_a_q_quote_do_not_leak_into_the_mask(self):
        masked, terminated = mask_sql("SELECT q'[don't drop this table]' FROM dual;")
        self.assertTrue(terminated)
        self.assertEqual(MUTATION.findall(masked), [])

    def test_even_numbers_of_embedded_apostrophes_are_masked_too(self):
        # The dangerous parity: the old lexer did not raise here, it silently
        # left the interior unmasked.
        masked, terminated = mask_sql("SELECT q'[it's Bob's DROP TABLE]' FROM dual;")
        self.assertTrue(terminated)
        self.assertEqual(MUTATION.findall(masked), [])

    def test_plain_literals_comments_and_identifiers_still_mask(self):
        masked, terminated = mask_sql(
            "SELECT 'a ''drop'' b', \"My Drop Col\" FROM t; -- drop tail\n/* drop block */\n"
        )
        self.assertTrue(terminated)
        self.assertEqual(MUTATION.findall(masked), [])

    def test_newlines_and_length_are_preserved(self):
        text = "SELECT 1\n  FROM dual;\n-- trailing\n"
        masked, _ = mask_sql(text)
        self.assertEqual(len(masked), len(text))
        self.assertEqual(masked.count("\n"), text.count("\n"))

    def test_unterminated_constructs_are_reported_not_raised(self):
        for text in ("SELECT 'x FROM dual;", "SELECT q'[x FROM dual;", "SELECT 1 /* x"):
            masked, terminated = mask_sql(text)
            self.assertFalse(terminated, text)
            self.assertEqual(len(masked), len(text))

    def test_q_preceded_by_an_identifier_character_is_not_a_q_quote(self):
        # myq'x' is the identifier myq followed by an ordinary literal.
        masked, terminated = mask_sql("SELECT myq'x' FROM dual;")
        self.assertTrue(terminated)
        self.assertIn("myq", masked)


class ClobBuilderTests(unittest.TestCase):
    def test_every_emitted_line_is_bounded(self):
        payload = "x" * 60000
        built = clob_builder("v_manifest", payload)
        self.assertTrue(built.splitlines())
        self.assertLess(max(len(line) for line in built.splitlines()), 1200)

    def test_builder_creates_then_appends_the_whole_value(self):
        built = clob_builder("v_x", "abc")
        self.assertIn("DBMS_LOB.CREATETEMPORARY(v_x, TRUE);", built)
        self.assertIn("DBMS_LOB.APPEND(v_x, TO_CLOB('abc'));", built)

    def test_empty_value_creates_an_empty_temporary(self):
        built = clob_builder("v_x", "")
        self.assertIn("CREATETEMPORARY", built)
        self.assertNotIn("APPEND", built)

    def test_quotes_are_doubled_and_nul_is_refused(self):
        self.assertIn("TO_CLOB('it''s')", clob_builder("v_x", "it's"))
        with self.assertRaises(SqlTextError):
            clob_builder("v_x", "bad\x00value")


class SqlLiteralTests(unittest.TestCase):
    def test_quotes_are_doubled_and_nul_is_refused(self):
        self.assertEqual(sql_literal("it's"), "'it''s'")
        with self.assertRaises(SqlTextError):
            sql_literal("bad\x00value")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_sql_text -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'teamlib.sql_text'`.

- [ ] **Step 3: Write the module**

Create `scripts/teamlib/sql_text.py`:

```python
"""Shared, Oracle-aware SQL text helpers.

These were previously private helpers in ``migration_store`` that
``control_store`` reached into by underscore name, and two independent
comment/literal lexers in ``migration_bundle`` and ``sqlcl``. One correct
implementation lives here so a fix reaches every caller.
"""

from __future__ import annotations

import base64
import re


class SqlTextError(ValueError):
    """Raised when a value cannot be embedded in generated SQL."""


_CLOB_CHUNK = 1000
_IDENTIFIER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$#"
)
# Oracle alternative quoting closes a bracket-style delimiter with its mate and
# every other delimiter with itself.
_Q_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}


def sql_literal(value: str) -> str:
    """Quote a value as a SQL string literal.

    Generated drivers always run under ``SET DEFINE OFF``, so doubling the
    single quote is sufficient; ``&`` substitution is never active.
    """
    if not isinstance(value, str) or "\x00" in value:
        raise SqlTextError("SQL value is invalid")
    return "'" + value.replace("'", "''") + "'"


def clob_builder(variable: str, value: str, *, indent: str = "  ") -> str:
    """Emit PL/SQL that fills ``variable`` from bounded lines.

    A single ``TO_CLOB(..) || TO_CLOB(..)`` expression puts the whole value on
    one line, which a large schema inventory pushes past what SQLcl will read.
    One append per line keeps every line under about 1050 characters, and
    building the value once lets a caller reference it more than once without
    re-emitting it.

    The temporary LOB is not freed: each ``run_sqlcl`` call is a fresh
    single-purpose session, and the session end reclaims it. Freeing it here
    would need an exception handler that would swallow the ORA-20xxx codes the
    callers match on.
    """
    if not isinstance(variable, str) or not variable.isidentifier():
        raise SqlTextError("CLOB variable name is invalid")
    if not isinstance(value, str) or "\x00" in value:
        raise SqlTextError("CLOB value is invalid")
    lines = [f"{indent}DBMS_LOB.CREATETEMPORARY({variable}, TRUE);"]
    for index in range(0, len(value), _CLOB_CHUNK):
        piece = sql_literal(value[index:index + _CLOB_CHUNK])
        lines.append(f"{indent}DBMS_LOB.APPEND({variable}, TO_CLOB({piece}));")
    return "\n".join(lines)


def b64_sql(column: str) -> str:
    """Base64-encode a column so wrapped output stays safely re-joinable."""
    return (
        "UTL_RAW.CAST_TO_VARCHAR2(UTL_ENCODE.BASE64_ENCODE("
        f"UTL_RAW.CAST_TO_RAW(NVL({column}, CHR(1)))))"
    )


def _decode_b64(value: str) -> str:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
        return "" if decoded == "\x01" else decoded
    except (ValueError, UnicodeError) as exc:
        raise SqlTextError("metadata row contains invalid encoded text") from exc


def row_lines(stdout: str, prefix: str) -> list[list[str]]:
    """Parse prefixed, base64-framed metadata rows from SQLcl output."""
    rows: list[list[str]] = []
    pending: str | None = None

    def consume(value: str) -> None:
        parts = value.split("|")[1:]
        if not parts or any(not part for part in parts):
            raise SqlTextError(f"malformed {prefix} metadata row")
        rows.append([_decode_b64(part) for part in parts])

    for raw in stdout.splitlines():
        line = raw.strip().rstrip("\r")
        if line.startswith(prefix):
            if pending is not None:
                consume(pending)
            pending = line
            continue
        # SQLcl may wrap a long SELECT expression at its terminal width even
        # after LINESIZE is raised. The encoded payload deliberately contains
        # only base64 characters and separators, so continuation lines can be
        # joined without accepting arbitrary diagnostic output.
        if pending is not None and re.fullmatch(r"[A-Za-z0-9+/=|]+", line):
            pending += line
    if pending is not None:
        consume(pending)
    return rows


def _mask_span(chars: list[str], text: str, start: int, stop: int) -> None:
    for index in range(start, stop):
        if text[index] != "\n":
            chars[index] = " "


def mask_sql(text: str) -> tuple[str, bool]:
    """Blank comments and literals, preserving offsets and newlines.

    Returns the masked text and whether every construct was terminated. Oracle
    alternative quoting (``q'[...]'``, ``nq'{...}'``) is recognised, so an
    embedded apostrophe neither ends the literal early nor leaks the rest of
    the literal's text into the masked output as if it were code.
    """
    chars = list(text)
    index = 0
    length = len(text)
    while index < length:
        current = text[index]
        following = text[index + 1] if index + 1 < length else ""

        if current == "-" and following == "-":
            while index < length and text[index] != "\n":
                chars[index] = " "
                index += 1
            continue

        if current == "/" and following == "*":
            chars[index] = chars[index + 1] = " "
            index += 2
            while index < length:
                if text[index] == "*" and index + 1 < length and text[index + 1] == "/":
                    chars[index] = chars[index + 1] = " "
                    index += 2
                    break
                if text[index] != "\n":
                    chars[index] = " "
                index += 1
            else:
                return "".join(chars), False
            continue

        if current in {"q", "Q"} and following == "'":
            previous = text[index - 1] if index else ""
            if previous in {"n", "N"} and (index < 2 or text[index - 2] not in _IDENTIFIER_CHARS):
                start = index - 1
            elif previous not in _IDENTIFIER_CHARS:
                start = index
            else:
                start = None
            if start is not None and index + 2 < length:
                delimiter = text[index + 2]
                closer = _Q_CLOSERS.get(delimiter, delimiter)
                end = text.find(closer + "'", index + 3)
                if end == -1:
                    _mask_span(chars, text, start, length)
                    return "".join(chars), False
                _mask_span(chars, text, start, end + 2)
                index = end + 2
                continue

        if current in {"'", '"'}:
            quote = current
            chars[index] = " "
            index += 1
            while index < length:
                if text[index] == quote:
                    if index + 1 < length and text[index + 1] == quote:
                        chars[index] = chars[index + 1] = " "
                        index += 2
                        continue
                    chars[index] = " "
                    index += 1
                    break
                if text[index] != "\n":
                    chars[index] = " "
                index += 1
            else:
                return "".join(chars), False
            continue

        index += 1
    return "".join(chars), True
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_sql_text -v
```

Expected: OK, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/teamlib/sql_text.py scripts/tests/test_sql_text.py
git commit -m "$(cat <<'EOF'
Add a shared Oracle-aware SQL text module

Two independent comment/literal lexers both understood '...' but not Oracle
alternative quoting, so q'[don't touch]' either aborted bundle loading or
leaked its interior into the production read-only guard's keyword scan. One
masker lives here, alongside the SQL helpers control_store previously imported
from migration_store by underscore name. Callers are rewired next.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Rewire both lexers and both stores onto the shared module

**Files:**
- Modify: `scripts/teamlib/migration_bundle.py:53-113` (`_mask_code`)
- Modify: `scripts/teamlib/sqlcl.py:52-57` (`_mask_sql_comments_and_literals`)
- Modify: `scripts/teamlib/migration_store.py:46-137` (delete the moved helpers, re-export)
- Modify: `scripts/teamlib/control_store.py:30` (import from `sql_text`)
- Test: `scripts/tests/test_migration_bundle.py`, `scripts/tests/test_sqlcl.py`

**Interfaces:**
- Consumes: `mask_sql`, `sql_literal`, `b64_sql`, `row_lines`, `SqlTextError`
  from Task 5.
- Produces: `migration_bundle._mask_code(text) -> str` keeps its signature and
  its `BundleError` on unterminated input. `sqlcl._assert_production_read_only`
  keeps its signature and now also refuses unterminated input.

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_migration_bundle.py`:

```python
    def test_q_quoted_literals_load_without_a_false_unterminated_error(self):
        from teamlib.migration_bundle import _mask_code
        for text in (
            "UPDATE t SET msg = q'[don't touch]' WHERE id = 1;",
            "UPDATE t SET msg = q'{it's a test}' WHERE id = 1;",
        ):
            masked = _mask_code(text)
            self.assertEqual(len(masked), len(text))
            self.assertNotIn("touch", masked)

    def test_genuinely_unterminated_literals_still_fail(self):
        from teamlib.migration_bundle import _mask_code, BundleError
        with self.assertRaisesRegex(BundleError, "unterminated"):
            _mask_code("UPDATE t SET msg = 'never closed;")
```

Append to `scripts/tests/test_sqlcl.py`, inside `SqlclBoundaryTests`:

```python
    def test_q_quoted_literal_does_not_trigger_a_false_mutation_refusal(self):
        from teamlib.sqlcl import _assert_production_read_only
        # No exception: the keyword lives inside the literal.
        _assert_production_read_only("SELECT q'[don't drop this table]' FROM dual;")

    def test_unterminated_literal_is_refused_on_a_production_read(self):
        from teamlib.sqlcl import _assert_production_read_only, SqlclError
        with self.assertRaisesRegex(SqlclError, "unterminated"):
            _assert_production_read_only("SELECT 'never closed FROM dual;")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_migration_bundle scripts.tests.test_sqlcl -v 2>&1 | tail -20
```

Expected: `BundleError: unterminated SQL comment or literal` from the first
test, and no exception raised in the sqlcl test where one is expected.

- [ ] **Step 3: Rewire the callers**

In `scripts/teamlib/migration_bundle.py`, delete the whole body of `_mask_code`
(lines 53-113) and replace it with:

```python
def _mask_code(text: str) -> str:
    """Mask strings and comments while retaining positions/newlines."""
    masked, terminated = mask_sql(text)
    if not terminated:
        raise BundleError("unterminated SQL comment or literal")
    return masked
```

Add to that module's imports:

```python
from .sql_text import mask_sql
```

In `scripts/teamlib/sqlcl.py`, delete `_mask_sql_comments_and_literals`
(lines 52-57) and change `_assert_production_read_only` to:

```python
def _assert_production_read_only(driver_text: str) -> None:
    masked, terminated = mask_sql(driver_text)
    if not terminated:
        raise SqlclError(
            "production read-only SQLcl operation has an unterminated comment or literal"
        )
    forbidden = re.search(
        r"\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|COMMIT|"
        r"ROLLBACK|GRANT|REVOKE|BEGIN|DECLARE|EXEC|EXECUTE)\b",
        masked,
        re.IGNORECASE,
    )
    if forbidden:
        raise SqlclError(
            "production read-only SQLcl operation contains a mutation/control statement: "
            + forbidden.group(0)
        )
```

Add to `scripts/teamlib/sqlcl.py` imports:

```python
from .sql_text import mask_sql
```

In `scripts/teamlib/migration_store.py`, delete `_sql_literal`, `_clob_literal`,
`_b64_sql`, `_decode_b64` and `_row_lines`, and re-export the shared names so
existing call sites inside the module keep working:

```python
from .sql_text import (
    SqlTextError,
    b64_sql as _b64_sql,
    clob_builder as _clob_builder,
    row_lines as _row_lines,
    sql_literal as _sql_literal,
)
```

Wrap the two entry points that previously raised `MigrationStoreError` for bad
values so the error type does not change for callers — add near the top of the
module:

```python
class MigrationStoreError(RuntimeError):
    """Raised when the migration metadata store cannot safely continue."""
```

(that class already exists; leave it) and make `SqlTextError` a recognised cause
by adding it to the `except` clause in `SqlMigrationStore._run`:

```python
        except SqlTextError as exc:
            raise MigrationStoreError(str(exc)) from exc
```

In `scripts/teamlib/control_store.py`, replace the private cross-module import:

```python
from .sql_text import b64_sql as _b64_sql, row_lines as _row_lines, sql_literal as _sql_literal
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK, all tests including the four new ones.

- [ ] **Step 5: Confirm the reported reproduction is fixed**

```bash
PYTHONPATH=scripts python3 -c "
from teamlib.sqlcl import _assert_production_read_only as chk
from teamlib.migration_bundle import _mask_code
_mask_code(\"UPDATE t SET m = q'[don't touch]' WHERE id=1;\"); print('bundle q-quote: ok')
chk(\"SELECT q'[don't drop this table]' FROM dual;\"); print('guard q-quote: ok')
"
```

Expected: both lines print `ok`.

- [ ] **Step 6: Commit**

```bash
git add scripts/teamlib/ scripts/tests/
git commit -m "$(cat <<'EOF'
Route both SQL lexers and both stores through sql_text

migration_bundle refused valid q-quoted SQL as unterminated, and the production
read-only guard matched keywords that lived inside a q-quote its masker had not
understood. Both now use the shared Oracle-aware masker, and an unterminated
construct in a production read is refused rather than scanned. control_store no
longer imports underscore-private helpers across a module boundary.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Anchor the control-command check to statement starts

`_assert_controls` searches masked code for
`\b(?:CONNECT|CONN|HOST|EXIT|WHENEVER)\b` anywhere. SQLcl client commands only
execute at the start of a statement, and the include check two lines above is
already correctly anchored — this one is not. It therefore rejects
`CONNECT BY PRIOR` (Oracle hierarchical queries), `EXIT WHEN` (PL/SQL loops), and
any `host` column — including the one in the template's own
`scripts/sql/control_metadata.sql:15`.

Line anchoring alone is not enough: `EXIT WHEN` sits at the start of a line
inside a PL/SQL block, and a formatted query can put `CONNECT BY` at the start of
a continuation line. The check must run only on lines that *begin a new
top-level statement*.

**Files:**
- Modify: `scripts/teamlib/migration_bundle.py:215-225` (`_assert_controls`)
- Test: `scripts/tests/test_migration_bundle.py`

**Interfaces:**
- Consumes: `_mask_code` from Task 6.
- Produces: `_statement_leading_lines(masked: str) -> Iterator[tuple[int, str]]`
  in `migration_bundle`. No later task depends on it.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_migration_bundle.py`:

```python
    def test_valid_oracle_constructs_are_not_mistaken_for_client_commands(self):
        from teamlib.migration_bundle import _assert_controls
        for sql in (
            "SELECT id FROM org\nSTART WITH id = 1\nCONNECT BY PRIOR id = parent_id;\n",
            "BEGIN\n  FOR r IN (SELECT 1 x FROM dual) LOOP\n    EXIT WHEN r.x > 5;\n  END LOOP;\nEND;\n/\n",
            "CREATE TABLE nodes (host VARCHAR2(255));\n",
            "CREATE TABLE t (\n  host VARCHAR2(512) NOT NULL,\n  id NUMBER\n);\n",
            "CREATE OR REPLACE PROCEDURE p IS\nBEGIN\n  EXIT;\nEND;\n/\n",
        ):
            _assert_controls(sql)

    def test_the_template_own_control_metadata_ddl_is_a_valid_migration_member(self):
        from teamlib.migration_bundle import _assert_controls
        root = Path(__file__).resolve().parents[2]
        _assert_controls((root / "scripts/sql/control_metadata.sql").read_text(encoding="utf-8"))

    def test_real_client_commands_are_still_rejected(self):
        from teamlib.migration_bundle import _assert_controls, BundleError
        for sql in (
            "HOST rm -rf /tmp/x\n",
            "SELECT 1 FROM dual;\nEXIT\n",
            "SELECT 1 FROM dual;\nWHENEVER SQLERROR CONTINUE\n",
            "SELECT 1 FROM dual;\nCONNECT scott/tiger@db\n",
            "SELECT 1 FROM dual;\nSPOOL /tmp/out.txt\n",
        ):
            with self.assertRaises(BundleError, msg=sql):
                _assert_controls(sql)
```

Add `from pathlib import Path` to that test file's imports if it is not present.

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_migration_bundle -v 2>&1 | tail -20
```

Expected: `BundleError: SQLcl control command is prohibited in migration members`
on the first valid construct.

- [ ] **Step 3: Implement the statement-boundary scanner**

In `scripts/teamlib/migration_bundle.py`, add above `_assert_controls`:

```python
# SQLcl interprets a client command only where a new statement begins. Matching
# these words anywhere in a body rejects CONNECT BY, EXIT WHEN and any column
# named host -- including this template's own control_metadata.sql.
_CLIENT_COMMAND_RE = re.compile(
    r"^(?:@{1,2}|!)|^(?:CONNECT|CONN|HOST|EXIT|QUIT|WHENEVER|SPOOL|SCRIPT|START)\b",
    re.IGNORECASE,
)
_BLOCK_START_RE = re.compile(
    r"^(?:DECLARE|BEGIN)\b"
    r"|^CREATE(?:\s+OR\s+REPLACE)?\s+(?:PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE)\b",
    re.IGNORECASE,
)


def _statement_leading_lines(masked: str):
    """Yield (line number, text) for lines that begin a top-level statement.

    A PL/SQL block is one statement terminated by a line containing only "/",
    so nothing inside it can be a client command. Outside a block, a statement
    begins after a ";" or "/" -- which is why a continuation line carrying
    CONNECT BY is never offered to the caller.
    """
    pending = True
    in_block = False
    for number, raw in enumerate(masked.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if in_block:
            if line == "/":
                in_block = False
                pending = True
            continue
        if pending:
            yield number, line
            if _BLOCK_START_RE.match(line):
                in_block = True
                pending = False
                continue
        pending = line.endswith(";") or line == "/"
```

Replace the unanchored search in `_assert_controls` with:

```python
    for number, line in _statement_leading_lines(code):
        if _CLIENT_COMMAND_RE.match(line):
            raise BundleError(
                f"SQLcl control command is prohibited in migration members (line {number})"
            )
```

Delete the now-redundant nested-include check immediately above it — the
`@`/`@@`/`START`/`SCRIPT` cases are covered by `_CLIENT_COMMAND_RE`. Keep its
distinct message by special-casing it:

```python
    for number, line in _statement_leading_lines(code):
        if re.match(r"^(?:@{1,2}|START\b|SCRIPT\b)", line, re.IGNORECASE):
            raise BundleError(f"nested SQLcl includes are prohibited (line {number})")
        if _CLIENT_COMMAND_RE.match(line):
            raise BundleError(
                f"SQLcl control command is prohibited in migration members (line {number})"
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_migration_bundle -v
```

Expected: OK. If
`test_control_commands_and_nested_includes_are_rejected` fails, its fixture may
place a control command mid-line; update the fixture to put it at a statement
start, which is the only position where SQLcl would execute it.

- [ ] **Step 5: Run the full suite**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK.

- [ ] **Step 6: Commit**

```bash
git add scripts/teamlib/migration_bundle.py scripts/tests/test_migration_bundle.py
git commit -m "$(cat <<'EOF'
Check SQLcl control commands only at statement starts

The word-boundary search rejected CONNECT BY, EXIT WHEN and every column named
host, so this template's own control_metadata.sql could not be authored as a
migration bundle. Client commands only execute where a statement begins, so
scan those positions -- tracking PL/SQL blocks, which are one statement ending
at "/" -- exactly as the nested-include check beside it already did.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Make the production read-only guard an allowlist

The guard is a keyword denylist. Verified gaps: `HOST rm -rf /tmp/x`, `!id`,
`SCRIPT`, `@include`, `SPOOL`, `CONNECT`, `CALL`, `LOCK TABLE`, `SAVEPOINT` and
`SET TRANSACTION` all pass it. Production writes are refused in six other
modules, so this is the last of several layers — but it is the layer whose job is
catching a bad file. An allowlist fails closed on the next SQLcl feature.

**Files:**
- Modify: `scripts/teamlib/sqlcl.py` (`_assert_production_read_only`)
- Test: `scripts/tests/test_production_boundary.py`

**Interfaces:**
- Consumes: `mask_sql` from Task 5, `_statement_leading_lines` logic re-used in
  spirit but implemented locally (sqlcl must not import from `migration_bundle`).
- Produces: no new public names.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_production_boundary.py`:

```python
class ProductionReadAllowlistTests(unittest.TestCase):
    def test_client_and_mutation_constructs_are_refused(self):
        from teamlib.sqlcl import _assert_production_read_only, SqlclError
        for sql in (
            "HOST rm -rf /tmp/x\n",
            "!id\n",
            "SCRIPT var x = 1;\n",
            "@/tmp/other.sql\n",
            "SPOOL /tmp/out.txt\n",
            "CONNECT other/pw@db\n",
            "CALL my_proc();\n",
            "LOCK TABLE t IN EXCLUSIVE MODE;\n",
            "SAVEPOINT s1;\n",
            "SET TRANSACTION READ WRITE;\n",
            "INSERT INTO t VALUES (1);\n",
        ):
            with self.assertRaises(SqlclError, msg=sql):
                _assert_production_read_only(sql)

    def test_genuine_read_only_drivers_are_accepted(self):
        from teamlib.sqlcl import _assert_production_read_only
        for sql in (
            "SELECT 1 FROM dual;\n",
            "SET HEADING OFF\nSET PAGESIZE 0\nSELECT 1 FROM dual;\n",
            "WITH x AS (SELECT 1 a FROM dual) SELECT a FROM x;\n",
            "SELECT q'[don't drop this]' FROM dual;\n",
            "-- a comment\n/* another */\nSELECT 1 FROM dual;\n",
            "SELECT id FROM org START WITH id = 1 CONNECT BY PRIOR id = parent_id;\n",
        ):
            _assert_production_read_only(sql)

    def test_the_shipped_identity_driver_is_accepted(self):
        from teamlib.sqlcl import _assert_production_read_only
        root = Path(__file__).resolve().parents[2]
        _assert_production_read_only((root / "scripts/sql/identity.sql").read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_production_boundary -v 2>&1 | tail -20
```

Expected: `SqlclError not raised` for `HOST rm -rf /tmp/x`.

- [ ] **Step 3: Replace the denylist with an allowlist**

In `scripts/teamlib/sqlcl.py`, replace `_assert_production_read_only` with:

```python
# A production read may only issue queries and the display settings the driver
# needs. Anything else -- SQL the allowlist does not name, or a SQLcl client
# command such as HOST, SCRIPT, @, SPOOL or CONNECT -- is refused. An allowlist
# fails closed on a SQLcl feature that does not exist yet; a denylist does not.
_PRODUCTION_READ_ALLOWED_RE = re.compile(r"^(?:SELECT|WITH)\b", re.IGNORECASE)
_PRODUCTION_READ_SETTINGS_RE = re.compile(
    r"^SET\s+(?:HEADING|FEEDBACK|LINESIZE|PAGESIZE|LONG|ECHO|VERIFY|DEFINE|ENCODING|"
    r"TERMOUT|TRIMSPOOL|SQLBLANKLINES|MARKUP)\b",
    re.IGNORECASE,
)
_PRODUCTION_READ_DIRECTIVE_RE = re.compile(r"^(?:WHENEVER\s+(?:SQLERROR|OSERROR)\b|EXIT\b)", re.IGNORECASE)


def _assert_production_read_only(driver_text: str) -> None:
    masked, terminated = mask_sql(driver_text)
    if not terminated:
        raise SqlclError(
            "production read-only SQLcl operation has an unterminated comment or literal"
        )
    pending = True
    in_block = False
    for number, raw in enumerate(masked.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if in_block:
            if line == "/":
                in_block = False
                pending = True
            continue
        if pending:
            if _PRODUCTION_READ_ALLOWED_RE.match(line):
                pass
            elif _PRODUCTION_READ_SETTINGS_RE.match(line):
                pass
            elif _PRODUCTION_READ_DIRECTIVE_RE.match(line):
                pass
            else:
                raise SqlclError(
                    "production read-only SQLcl operation contains a statement that is not a "
                    f"query or a display setting (line {number})"
                )
        pending = line.endswith(";") or line == "/"
```

Because the generated driver in `_driver_text` opens with `SET DEFINE OFF` and
`WHENEVER SQLERROR EXIT SQL.SQLCODE` and closes with `EXIT`, those forms are
allowed explicitly above. The guard runs against the *payload*, not the
generated driver, but keeping the settings and directives allowed means an
operator-supplied read driver can carry the same preamble.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_production_boundary scripts.tests.test_sqlcl -v
```

Expected: OK.

- [ ] **Step 5: Re-run the review's reproduction table**

```bash
PYTHONPATH=scripts python3 -c "
from teamlib.sqlcl import _assert_production_read_only as chk
bad=[\"HOST rm -rf /tmp/x\",'!id','SCRIPT var x=1;','@/tmp/other.sql','SPOOL /tmp/o.txt',
     'CONNECT o/p@db','CALL my_proc();','LOCK TABLE t IN EXCLUSIVE MODE;','SAVEPOINT s1;',
     'SET TRANSACTION READ WRITE;']
for s in bad:
    try: chk(s); print('  STILL PASSES:', s)
    except Exception: print('  blocked     :', s)
"
```

Expected: every line reports `blocked`.

- [ ] **Step 6: Commit**

```bash
git add scripts/teamlib/sqlcl.py scripts/tests/test_production_boundary.py
git commit -m "$(cat <<'EOF'
Require an allowlist for production read-only drivers

The keyword denylist let HOST, !, SCRIPT, @, SPOOL, CONNECT, CALL, LOCK TABLE,
SAVEPOINT and SET TRANSACTION through, because SQLcl client commands are not SQL
keywords. Require every top-level statement to be a query or a display setting
instead, so an unrecognised construct fails closed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

# Phase 3 — Scale and timeouts

## Task 9: Build metadata CLOBs from bounded lines

`_clob_literal` joins 1000-character pieces with `" || "` and no newlines. A
400-object inventory manifest measures 44,425 characters and produces a
**45,096-character single line**, emitted **twice** in the same payload (the
`INSERT` and the `DBMS_LOB.COMPARE` branch). `SET LINESIZE 32767` is output
width, not input line length. The read path already chunks at 900 characters
with an explicit part/total — this applies the same discipline to writes, and
building the value once also halves the payload.

**Files:**
- Modify: `scripts/teamlib/migration_store.py:296-342` (`record_inventory`),
  `:592-615` (`record_applied`)
- Test: `scripts/tests/test_sql_metadata_store.py`

**Interfaces:**
- Consumes: `clob_builder` from Task 5 (imported as `_clob_builder` in Task 6).
- Produces: no new public names. `_clob_literal` is deleted.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_sql_metadata_store.py`:

```python
class GeneratedSqlLineLengthTests(unittest.TestCase):
    MAX_LINE = 1200

    def _capture_payload(self, call):
        seen: list[str] = []

        def runner(target, operation, driver, work, **kwargs):
            seen.append(Path(driver).read_text(encoding="utf-8"))
            raise RuntimeError("payload captured")

        try:
            call(runner)
        except Exception:
            pass
        return seen

    def test_a_large_inventory_manifest_emits_only_bounded_lines(self):
        from teamlib.sql_text import clob_builder
        manifest = json.dumps(
            {"version": 1, "objects": [
                {"name": f"TBL_{i:04d}", "type": "TABLE", "sha256": "a" * 64} for i in range(400)
            ]},
            sort_keys=True, separators=(",", ":"),
        )
        built = clob_builder("v_manifest", manifest)
        self.assertGreater(len(manifest), 40000)
        self.assertLess(max(len(line) for line in built.splitlines()), self.MAX_LINE)
        # The value is emitted once, not once per reference.
        self.assertEqual(built.count("CREATETEMPORARY"), 1)

    def test_clob_literal_is_gone(self):
        import teamlib.migration_store as store
        self.assertFalse(hasattr(store, "_clob_literal"))
```

Add `import json` and `from pathlib import Path` to that test file if absent.

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_sql_metadata_store -v 2>&1 | tail -20
```

Expected: `AssertionError: True is not false` on `test_clob_literal_is_gone`.

- [ ] **Step 3: Rewrite `record_inventory`**

In `scripts/teamlib/migration_store.py`, delete `_clob_literal`. Replace the
payload in `record_inventory` with:

```python
        payload = f"""
DECLARE
  v_owned NUMBER;
  v_existing NUMBER;
  v_compare INTEGER;
  v_schema_set VARCHAR2(64);
  v_meta_schema_set VARCHAR2(64);
  v_normalizer VARCHAR2(32);
  v_coverage VARCHAR2(32);
  v_manifest CLOB;
BEGIN
{_clob_builder('v_manifest', manifest_json)}
  SELECT COUNT(*) INTO v_owned FROM TEAM_MIGRATION_MUTEX
   WHERE singleton_id = 1 AND owner_token = {_sql_literal(run_token)};
  IF v_owned = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'INVENTORY_WRITE_REFUSED'); END IF;
  SELECT schema_set_digest INTO v_meta_schema_set
    FROM TEAM_MIGRATION_META
   WHERE version_number = 1 AND project_id = {_sql_literal(store_target.project)};
  IF v_meta_schema_set <> {_sql_literal(str(manifest['schema_set_digest']))} THEN
    RAISE_APPLICATION_ERROR(-20011, 'SCHEMA_SET_DIGEST_MISMATCH');
  END IF;
  SELECT COUNT(*) INTO v_existing FROM TEAM_MIGRATION_INVENTORY
   WHERE inventory_digest = {_sql_literal(digest)};
  IF v_existing = 0 THEN
    INSERT INTO TEAM_MIGRATION_INVENTORY
      (inventory_digest, manifest_json, schema_set_digest, normalizer_version, coverage_version)
    VALUES ({_sql_literal(digest)}, v_manifest,
            {_sql_literal(str(manifest['schema_set_digest']))},
            {_sql_literal(str(manifest['normalizer_version']))},
            {_sql_literal(str(manifest['coverage_version']))});
  ELSE
    SELECT DBMS_LOB.COMPARE(manifest_json, v_manifest),
           schema_set_digest, normalizer_version, coverage_version
      INTO v_compare, v_schema_set, v_normalizer, v_coverage
      FROM TEAM_MIGRATION_INVENTORY
     WHERE inventory_digest = {_sql_literal(digest)};
    IF NVL(v_compare, -1) <> 0
       OR v_schema_set <> {_sql_literal(str(manifest['schema_set_digest']))}
       OR v_normalizer <> {_sql_literal(str(manifest['normalizer_version']))}
       OR v_coverage <> {_sql_literal(str(manifest['coverage_version']))}
    THEN
      RAISE_APPLICATION_ERROR(-20007, 'INVENTORY_IMMUTABILITY_VIOLATION');
    END IF;
  END IF;
  COMMIT;
END;
/
"""
```

Note the ordering change: the CLOB is built before the ownership check. That is
safe — building a session-local temporary LOB writes nothing to any table, and
the `INVENTORY_WRITE_REFUSED` check still gates every `INSERT`.

- [ ] **Step 4: Rewrite the `record_applied` CLOBs**

In the same file, add the two variables to that block's `DECLARE` section:

```
  v_dependencies CLOB;
  v_observation CLOB;
```

Immediately after its `BEGIN`, insert:

```python
{_clob_builder('v_dependencies', dependency_json)}
{_clob_builder('v_observation', observation_json)}
```

and change the `INSERT` values from
`{_clob_literal(dependency_json)}, {_clob_literal(observation_json)}` to:

```
          v_dependencies, v_observation,
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK.

- [ ] **Step 6: Measure the improvement**

```bash
PYTHONPATH=scripts python3 -c "
import json
from teamlib.sql_text import clob_builder
m=json.dumps({'version':1,'objects':[{'name':f'TBL_{i:04d}','type':'TABLE','sha256':'a'*64} for i in range(400)]},sort_keys=True,separators=(',',':'))
b=clob_builder('v_manifest', m)
print('manifest chars      :', len(m))
print('generated SQL chars :', len(b))
print('max line length     :', max(len(l) for l in b.splitlines()))
print('emitted copies      :', b.count('CREATETEMPORARY'))
"
```

Expected: max line length near 1030, one emitted copy.

- [ ] **Step 7: Commit**

```bash
git add scripts/teamlib/migration_store.py scripts/tests/test_sql_metadata_store.py
git commit -m "$(cat <<'EOF'
Build metadata CLOBs from bounded lines instead of one expression

A 400-object inventory manifest produced a 45,096-character single SQL line,
emitted twice per payload for the INSERT and the DBMS_LOB.COMPARE. The read
path already chunked CLOBs at 900 characters; writes now use DBMS_LOB.APPEND
one statement per line and build each value once, which also halves the
payload.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Make the SQLcl timeout configurable and generous for APEX

`run_sqlcl` hardcodes `timeout: float = 120.0` and no caller overrides it, so a
full `APEX EXPORT`/`IMPORT` runs on the same budget as an identity probe. The
failure is safe — `import_app` retains the owner token on `SqlclError` — but that
means a slow import leaves the shared application `is_uncertain = 1`, requiring a
named recovery owner before anyone can work.

**Files:**
- Modify: `scripts/teamlib/sqlcl.py` (timeout resolution),
  `scripts/teamlib/apex.py:191, :465` (pass the APEX budget)
- Modify: `.env.example`, `docs/toolchain.md`
- Test: `scripts/tests/test_sqlcl.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `sqlcl.DEFAULT_TIMEOUT_SECONDS: float = 120.0`
  - `sqlcl.APEX_TIMEOUT_SECONDS: float = 1800.0`
  - `run_sqlcl(..., timeout: float | None = None)` — `None` resolves from
    `TEAM_SQLCL_TIMEOUT`, else `DEFAULT_TIMEOUT_SECONDS`.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_sqlcl.py`, inside `SqlclBoundaryTests`:

```python
    def test_timeout_defaults_and_env_override_resolve(self):
        from teamlib.sqlcl import _resolve_timeout, DEFAULT_TIMEOUT_SECONDS, SqlclError
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEAM_SQLCL_TIMEOUT", None)
            self.assertEqual(_resolve_timeout(None), DEFAULT_TIMEOUT_SECONDS)
            self.assertEqual(_resolve_timeout(45.0), 45.0)
        with patch.dict(os.environ, {"TEAM_SQLCL_TIMEOUT": "900"}):
            self.assertEqual(_resolve_timeout(None), 900.0)
            # An explicit argument still wins over the environment.
            self.assertEqual(_resolve_timeout(45.0), 45.0)
        for bad in ("0", "-5", "abc", ""):
            with patch.dict(os.environ, {"TEAM_SQLCL_TIMEOUT": bad}):
                with self.assertRaises(SqlclError):
                    _resolve_timeout(None)

    def test_apex_operations_request_the_long_budget(self):
        import inspect
        from teamlib import apex
        from teamlib.sqlcl import APEX_TIMEOUT_SECONDS
        self.assertGreaterEqual(APEX_TIMEOUT_SECONDS, 900.0)
        source = inspect.getsource(apex)
        self.assertIn("APEX_TIMEOUT_SECONDS", source)
```

Add `import os` and `from unittest.mock import patch` to that test file if absent.

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_sqlcl -v 2>&1 | tail -20
```

Expected: `ImportError: cannot import name '_resolve_timeout'`.

- [ ] **Step 3: Implement the resolver**

In `scripts/teamlib/sqlcl.py`, add near the module constants:

```python
DEFAULT_TIMEOUT_SECONDS = 120.0
# A full APEX application export or import is not comparable to a metadata
# query. Timing one out marks the shared application uncertain, which stops the
# whole team until a named recovery owner reviews evidence, so the budget has to
# fit a real application over a real network.
APEX_TIMEOUT_SECONDS = 1800.0


def _resolve_timeout(timeout: float | None) -> float:
    if timeout is not None:
        return float(timeout)
    raw = os.environ.get("TEAM_SQLCL_TIMEOUT")
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise SqlclError("TEAM_SQLCL_TIMEOUT must be a positive number of seconds") from exc
    if value <= 0:
        raise SqlclError("TEAM_SQLCL_TIMEOUT must be a positive number of seconds")
    return value
```

Change the signature and the first line of the body:

```python
def run_sqlcl(
    target: Target,
    operation: str,
    driver: str | Path,
    work: str | Path,
    *,
    executable: str | Path | None = None,
    timeout: float | None = None,
) -> SqlResult:
    """Run one verified SQLcl process with a regular empty stdin file."""
    resolved_timeout = _resolve_timeout(timeout)
```

and use `resolved_timeout` in the `subprocess.run(... timeout=resolved_timeout)`
call.

- [ ] **Step 4: Pass the APEX budget at the two APEX call sites**

In `scripts/teamlib/apex.py`, add to the imports:

```python
from .sqlcl import APEX_TIMEOUT_SECONDS, SqlclError, run_sqlcl
```

At `capture_app` (line 191), change the call to:

```python
        result = runner(target, "read", driver, work, timeout=APEX_TIMEOUT_SECONDS)
```

At `import_app` (line 465):

```python
        result = runner(target, "write", driver, work, timeout=APEX_TIMEOUT_SECONDS)
```

Both `runner` parameters default to `run_sqlcl`. Every fixture on the APEX path
already accepts `**kwargs` — `test_export_app.py:52`, `test_import_app.py:58`
and `:101`, `test_recovery_flow.py:59` — so none needs changing. The two
fixtures that take a fixed signature, `test_masters.py:96`/`:110` and
`test_live_inventory.py:37`, serve `masters.py` and `live_inventory.py`, which
this task does not modify. Do not add `timeout=` to any call site in those
modules as part of this task; they inherit the default and the
`TEAM_SQLCL_TIMEOUT` override like everything else.

- [ ] **Step 5: Document the knob**

Append to `.env.example`:

```
# Optional. Seconds allowed for one SQLcl process. Unset uses 120s for metadata
# work; APEX export/import always uses the longer built-in application budget.
# TEAM_SQLCL_TIMEOUT=600
```

In `docs/toolchain.md`, after the paragraph beginning "Before a live run", add:

```markdown
One SQLcl process is bounded by `TEAM_SQLCL_TIMEOUT` seconds, defaulting to 120.
APEX export and import use a longer built-in budget because a timeout there
marks the shared application uncertain and pauses the team. Raise the variable
for a slow link; do not lower it below the time a metadata write needs.
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK.

- [ ] **Step 7: Commit**

```bash
git add scripts/teamlib/sqlcl.py scripts/teamlib/apex.py .env.example docs/toolchain.md scripts/tests/test_sqlcl.py
git commit -m "$(cat <<'EOF'
Give APEX operations a real timeout budget and an override

Every SQLcl call inherited a hardcoded 120 seconds, so a full application
export or import shared a budget with an identity probe. Timing out an import
marks the shared application uncertain and pauses the team, so APEX operations
now use a long built-in budget and every other call honours TEAM_SQLCL_TIMEOUT.
The fail-closed behaviour on timeout is unchanged.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

# Phase 4 — CI and packaging

## Task 11: Write CI replay evidence outside the checkout

`ci_replay_runner.py` resolves `repo = Path.cwd()` and refuses to run unless the
tree is pristine, allowing ignored paths only under `scratch/`, `.sync-state/`,
`.env` and `__pycache__`. `ci.py` launches it with no `cwd=`, so it inherits the
workspace — the same tree the workflow pipes into
`tee "evidence/ci/${SOURCE_SHA}.json"`. `evidence/` is not ignored, so the file
appears as untracked `??` and aborts the run. This is deterministic, not a race:
`tee` creates its output file at process start, long before the runner checks.
It is masked today only because the job's SQLcl precheck exits first on
`ubuntu-latest`.

**Files:**
- Modify: `.github/workflows/database-checks.yml:44-60`
- Test: `scripts/tests/test_ci_contract.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_ci_contract.py`:

```python
class ReplayWorkflowIsolationTests(unittest.TestCase):
    def test_replay_evidence_is_never_written_into_the_checkout(self):
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github/workflows/database-checks.yml").read_text(encoding="utf-8")
        # The runner refuses any untracked path outside its four allowed
        # prefixes, so evidence must not land in the working tree at all.
        self.assertNotIn('tee "evidence/', workflow)
        self.assertNotIn("mkdir -p evidence/ci", workflow)
        self.assertIn("RUNNER_TEMP", workflow)

    def test_runner_allowed_ignored_prefixes_are_documented_and_unchanged(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / "scripts/ci_replay_runner.py").read_text(encoding="utf-8")
        self.assertIn('allowed_ignored = ("scratch/", ".sync-state/", ".env", ".env.")', source)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_ci_contract -v 2>&1 | tail -20
```

Expected: `AssertionError: 'tee "evidence/' unexpectedly found in …`.

- [ ] **Step 3: Move the evidence out of the tree**

In `.github/workflows/database-checks.yml`, replace the "Replay selected SHA"
and "Publish exact-SHA qualification evidence" steps with:

```yaml
      - name: Replay selected SHA
        env:
          SOURCE_SHA: ${{ github.sha }}
        run: |
          # The replay runner refuses to run against a working tree carrying
          # untracked files, so evidence is written outside the checkout.
          mkdir -p "$RUNNER_TEMP/evidence"
          PYTHONPATH=scripts python3 scripts/team.py ci-replay \
            --ref "$SOURCE_SHA" \
            --contract ci/runner-contract.json \
            --provisioner provisioners/docker_pdb.sh \
            --runner ../scripts/ci_replay_runner.py \
            > "$RUNNER_TEMP/evidence/${SOURCE_SHA}.json"
          cat "$RUNNER_TEMP/evidence/${SOURCE_SHA}.json"

      - name: Publish exact-SHA qualification evidence
        if: success()
        uses: actions/upload-artifact@v4
        with:
          name: qualification-${{ github.sha }}
          path: ${{ runner.temp }}/evidence/${{ github.sha }}.json
          if-no-files-found: error
```

Using `>` rather than `| tee` also means a failing `ci-replay` fails the step:
in the original pipeline the exit status came from `tee`, so a non-zero
`ci-replay` was masked unless `pipefail` was set.

- [ ] **Step 4: Verify the workflow still parses**

```bash
python3 -c "
import json,sys
try:
    import yaml
except ImportError:
    print('pyyaml absent; skipping structural check'); sys.exit(0)
d=yaml.safe_load(open('.github/workflows/database-checks.yml'))
print('jobs:', list(d['jobs']))
"
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_ci_contract -v
```

Expected: the unittest run is OK. The YAML check is advisory.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/database-checks.yml scripts/tests/test_ci_contract.py
git commit -m "$(cat <<'EOF'
Write replay evidence outside the checkout

tee created evidence/ci/<sha>.json in the working tree at pipeline start, and
evidence/ is neither gitignored nor one of the replay runner's four allowed
prefixes, so the runner's own clean-checkout assertion aborted every replay on
a qualified runner. Redirect to RUNNER_TEMP and upload from there, which also
stops tee masking a failing ci-replay exit status.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Declare dependencies and stop masking a missing `cryptography`

Three runtime dependencies are declared nowhere. The consequential one is
`cryptography`, a hard requirement of `runbook.py` — and `gen-runbook` is the
production handoff command. Worse, its `try` block wraps the `import` statements
and catches bare `Exception`, so a missing package is reported as
"trusted production handoff key is not a readable PEM public key". An operator is
told their trust key is corrupt when a package is absent.

**Files:**
- Modify: `pyproject.toml` (created in Task 3)
- Modify: `scripts/teamlib/runbook.py:57-67`
- Modify: `README.md`
- Test: `scripts/tests/test_runbook.py`

**Interfaces:**
- Consumes: `pyproject.toml` from Task 3.
- Produces: `runbook.RunbookDependencyError(RunbookError)`.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_runbook.py`:

```python
class DependencyDiagnosticTests(unittest.TestCase):
    def test_missing_cryptography_is_reported_as_a_missing_dependency(self):
        import builtins
        from teamlib.runbook import _public_key, RunbookDependencyError

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ModuleNotFoundError("No module named 'cryptography'")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", refuse):
            with self.assertRaises(RunbookDependencyError) as caught:
                _public_key(b"-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA\n-----END PUBLIC KEY-----\n")
        message = str(caught.exception)
        self.assertIn("cryptography", message)
        self.assertNotIn("readable PEM", message)

    def test_a_genuinely_malformed_key_still_reports_a_key_problem(self):
        from teamlib.runbook import _public_key, RunbookError
        with self.assertRaisesRegex(RunbookError, "readable PEM"):
            _public_key(b"not a key at all")


class DependencyManifestTests(unittest.TestCase):
    def test_every_third_party_dependency_is_declared(self):
        root = Path(__file__).resolve().parents[2]
        manifest = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[project]", manifest)
        self.assertIn("cryptography", manifest)
        self.assertIn("graphify", manifest)
        self.assertIn("tree-sitter-sql", manifest)
        self.assertIn('requires-python = ">=3.10"', manifest)
```

Add `from unittest.mock import patch` and `from pathlib import Path` to that
file's imports if absent.

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_runbook -v 2>&1 | tail -20
```

Expected: `ImportError: cannot import name 'RunbookDependencyError'`.

- [ ] **Step 3: Separate the import failure from the parse failure**

In `scripts/teamlib/runbook.py`, add after the existing `RunbookError`:

```python
class RunbookDependencyError(RunbookError):
    """Raised when an optional signing dependency is not installed."""
```

Replace `_public_key`:

```python
def _public_key(raw: bytes):
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise RunbookDependencyError(
            "verifying a signed test handoff requires the 'cryptography' package; "
            "install it with: python3 -m pip install cryptography"
        ) from exc
    try:
        key = serialization.load_pem_public_key(raw)
    except Exception as exc:  # the precise cryptography exception varies by version
        raise RunbookError("trusted production handoff key is not a readable PEM public key") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise RunbookError("trusted production handoff key must be Ed25519")
    return key
```

- [ ] **Step 4: Declare the dependencies**

Prepend to `pyproject.toml`, above the existing `[tool.ruff]` table:

```toml
[project]
name = "apex-team-template"
version = "0.1.0"
description = "Shared-application APEX team workflow, migrations and promotion"
requires-python = ">=3.10"
# The daily team workflow -- export, reconcile, migrate, release -- is stdlib
# only and deliberately has no runtime dependencies.
dependencies = []

[project.optional-dependencies]
# Required by gen-runbook to verify the signed test evidence on the production
# handoff path.
promotion = ["cryptography>=41"]
# Required only by setup_graphify_apx.py and teamlib/graphify_corpus.py.
graph = ["graphify", "tree-sitter-sql"]
# Offline quality gate.
dev = ["ruff>=0.5"]
```

- [ ] **Step 5: Document it**

In `README.md`, after the "First setup" code block, add:

```markdown
The daily workflow needs no third-party packages. Two commands do:
`gen-runbook` needs `cryptography` to verify signed test evidence, and the
graphify tooling needs `graphify` and `tree-sitter-sql`. Install what you need:

```text
python3 -m pip install -e '.[promotion]'
```
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml scripts/teamlib/runbook.py README.md scripts/tests/test_runbook.py
git commit -m "$(cat <<'EOF'
Declare the three third-party dependencies and name a missing one

cryptography, graphify and tree-sitter-sql were declared nowhere. runbook.py
also wrapped its cryptography imports in the same try that catches a malformed
key, so a missing package told the operator on the production handoff path that
their trust key was unreadable. The imports now raise a dependency error naming
the package and the install command.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Pin one Python version across every CI job

`template-checks` pins 3.10, three workflows pin 3.11, and `integration.yml`'s
`qualification` job has no `setup-python` step at all. The launchers and the
release manifest both declare "3.10+", so 3.10 is the real floor and only one
job tests it.

**Files:**
- Modify: `.github/workflows/template-checks.yml`, `database-checks.yml`,
  `integration.yml`, `release.yml`
- Test: `scripts/tests/test_ci_contract.py`

**Interfaces:**
- Consumes: nothing. Produces: nothing.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_ci_contract.py`:

```python
class WorkflowPythonVersionTests(unittest.TestCase):
    FLOOR = "'3.10'"

    def test_every_job_running_team_py_pins_the_declared_floor(self):
        root = Path(__file__).resolve().parents[2]
        for name in ("template-checks", "database-checks", "integration", "release"):
            text = (root / ".github/workflows" / f"{name}.yml").read_text(encoding="utf-8")
            versions = re.findall(r"python-version:\s*(\S+)", text)
            self.assertTrue(versions, f"{name} pins no python-version")
            for version in versions:
                self.assertEqual(version, self.FLOOR, f"{name} pins {version}")
            # Any job invoking team.py must have set up Python first.
            if "team.py" in text:
                self.assertIn("actions/setup-python", text)

    def test_the_declared_floor_matches_the_launchers(self):
        root = Path(__file__).resolve().parents[2]
        self.assertIn("3.10+", (root / "scripts/team.sh").read_text(encoding="utf-8"))
        self.assertIn("3.10+", (root / "scripts/team.ps1").read_text(encoding="utf-8"))
```

Add `import re` to that test file if absent.

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_ci_contract -v 2>&1 | tail -20
```

Expected: `AssertionError: '3.11' != "'3.10'" : database-checks pins '3.11'`.

- [ ] **Step 3: Pin the floor everywhere**

Replace every `python-version: '3.11'` with `python-version: '3.10'` across the
four workflows.

In `.github/workflows/integration.yml`, add a setup step to the `qualification`
job, immediately after its `actions/checkout`:

```yaml
      - uses: actions/setup-python@v5
        with:
          python-version: '3.10'
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_ci_contract -v
```

Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ scripts/tests/test_ci_contract.py
git commit -m "$(cat <<'EOF'
Pin every workflow to the declared Python floor

template-checks pinned 3.10, three workflows pinned 3.11, and the integration
qualification job ran team.py on the runner default with no setup-python at
all. The launchers and the release manifest declare 3.10+, so that is the
version the gate must actually exercise.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

# Phase 5 — Operational hardening

## Task 14: Resolve the repository root instead of trusting `cwd`

`team.py:243` sets `repo = Path.cwd()`, and `team.sh` computes `REPO_ROOT` only
to locate `team.py` — it never `cd`s there. So `.env`, `.sync-state/`, `scratch/`
and `targets/` all resolve against whatever directory the developer is standing
in. It fails loudly, which is why this is not in Phase 1.

**Files:**
- Modify: `scripts/team.py` (add `_repo_root`, use it in `_online`)
- Test: `scripts/tests/test_launchers.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `team._repo_root() -> Path`.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_launchers.py`:

```python
class RepositoryRootTests(unittest.TestCase):
    def test_repo_root_is_stable_from_any_subdirectory(self):
        root = Path(__file__).resolve().parents[2]
        original = Path.cwd()
        try:
            os.chdir(root / "docs")
            self.assertEqual(team._repo_root().resolve(), root)
            os.chdir(root)
            self.assertEqual(team._repo_root().resolve(), root)
        finally:
            os.chdir(original)

    def test_repo_root_falls_back_to_the_script_parent_outside_a_repository(self):
        root = Path(__file__).resolve().parents[2]
        original = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="team-nonrepo-") as directory:
            try:
                os.chdir(directory)
                self.assertEqual(team._repo_root().resolve(), root)
            finally:
                os.chdir(original)
```

Add `import os` and `import tempfile` to that file if absent.

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_launchers -v 2>&1 | tail -20
```

Expected: `AttributeError: module 'team' has no attribute '_repo_root'`.

- [ ] **Step 3: Implement it**

In `scripts/team.py`, add above `_online`:

```python
def _repo_root() -> Path:
    """Resolve the repository root rather than trusting the caller's directory.

    Every online command reads .env and writes .sync-state/ and scratch/. Those
    belong to the repository, not to whatever directory the developer happened
    to be standing in when they ran the launcher.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return Path(__file__).resolve().parent.parent
```

Change the first line of `_online`:

```python
    repo = _repo_root()
```

Change `_env_path` so a relative `.env` also resolves against the root:

```python
def _env_path(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "env_file", None) or os.environ.get("PROJECT_ENV_FILE")
    if explicit:
        return Path(explicit)
    return _repo_root() / ".env"
```

- [ ] **Step 4: Verify from a subdirectory**

```bash
cp .env.example .env
( cd docs && ../scripts/team.sh doctor )
rm -f .env
```

Expected: the same JSON status object the command prints from the repository
root, not `could not read configuration: .env`.

- [ ] **Step 5: Run the full suite**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK.

- [ ] **Step 6: Commit**

```bash
git add scripts/team.py scripts/tests/test_launchers.py
git commit -m "$(cat <<'EOF'
Resolve the repository root instead of trusting the caller's directory

team.py used Path.cwd() as the repository, and team.sh computed REPO_ROOT only
to locate the script, so running any online command from a subdirectory looked
for .env, .sync-state, scratch and targets in the wrong place. Resolve through
git rev-parse with the script's parent as the fallback.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: Let `recover-app-lock` accept the run token it was given

When an import fails after the payload started but not because of SQLcl,
`import_app` calls `release_app(confirmed_success=False)`, which sets
`owner_token = NULL` while preserving `is_uncertain = 1`. `recover_app_lock`
then builds `owner_token = '<token>'` when `--run-token` is supplied, matching
zero rows and raising `RECOVERY_TOKEN_MISMATCH` — while omitting the token
succeeds. The tool rejects the operator who names the exact run they are
recovering.

**Files:**
- Modify: `scripts/teamlib/control_store.py:654` (`SqlControlStore.recover_app_lock`)
  and the matching JSON `ControlStore.recover_app_lock`
- Test: `scripts/tests/test_recovery_flow.py`

**Interfaces:**
- Consumes: `sql_literal` from Task 5.
- Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_recovery_flow.py`:

Add these two methods to the existing test class in that file, which already
builds `self.target`, `self.store` and `self.repo` in its `setUp`:

```python
    def test_supplying_the_run_token_of_a_released_uncertain_target_still_recovers(self):
        key = self.target.physical_key
        run_token = "a" * 32
        self.store.register_app(self.target, "checkout-1", "host-1", "dev")
        self.store.acquire_app(key, run_token, "checkout-1", "host-1", "dev")
        self.store.mark_payload_starting(key, run_token)
        # A non-SQLcl failure after the payload started clears the owner token
        # but leaves the target uncertain.
        self.store.release_app(key, run_token, confirmed_success=False)
        state = self.store.read_app_sync_state(key)
        self.assertIsNone(state.owner_token)
        self.assertTrue(state.is_uncertain)

        evidence = self.repo / "evidence.txt"
        evidence.write_text("worker terminated; capture retained\n", encoding="utf-8")
        recovered = self.store.recover_app_lock(key, evidence=evidence, run_token=run_token)
        self.assertFalse(recovered.is_uncertain)
        self.assertIsNone(recovered.owner_token)

    def test_a_run_token_that_never_held_the_target_is_still_refused(self):
        from teamlib.control_store import MutexHeld
        key = self.target.physical_key
        self.store.register_app(self.target, "checkout-1", "host-1", "dev")
        self.store.acquire_app(key, "a" * 32, "checkout-1", "host-1", "dev")
        self.store.mark_payload_starting(key, "a" * 32)
        evidence = self.repo / "evidence.txt"
        evidence.write_text("worker terminated\n", encoding="utf-8")
        with self.assertRaises(MutexHeld):
            self.store.recover_app_lock(key, evidence=evidence, run_token="b" * 32)
```

Both `ControlStore` and `SqlControlStore` expose `setup_state`, `register_app`,
`acquire_app`, `mark_payload_starting`, `release_app`, `recover_app_lock` and
`read_app_sync_state`, so the JSON store exercises the same state transitions
as the SQL one.

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_recovery_flow -v 2>&1 | tail -20
```

Expected: `MutexHeld: recovery token does not match the held app target`.

- [ ] **Step 3: Widen the predicate**

In `scripts/teamlib/control_store.py`, in `SqlControlStore.recover_app_lock`,
replace the predicate line with:

```python
        # A failure after the payload started clears owner_token but leaves the
        # target uncertain, so matching only on the token would refuse exactly
        # the operator who names the run they are recovering.
        predicate = (
            "owner_token IS NOT NULL OR is_uncertain = 1"
            if run_token is None
            else f"(owner_token = {_sql_literal(run_token)} "
                 f"OR (owner_token IS NULL AND is_uncertain = 1))"
        )
```

Apply the equivalent change to the JSON `ControlStore.recover_app_lock`: where it
compares `mutex.get("owner_token") != run_token`, accept the row when
`mutex.get("owner_token") is None and mutex.get("is_uncertain")` as well.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK.

- [ ] **Step 5: Document the behaviour**

In `docs/app-recovery.md`, in the `recover-app-lock` section, add:

```markdown
`--run-token` is optional. Supply it when the failure message or the recovery
journal names a run: it is accepted both while that run still owns the mutex and
after a failed attempt released the token but left the target uncertain.
Omitting it recovers any held or uncertain target for the alias.
```

- [ ] **Step 6: Commit**

```bash
git add scripts/teamlib/control_store.py docs/app-recovery.md scripts/tests/test_recovery_flow.py
git commit -m "$(cat <<'EOF'
Accept a named run token when recovering a released uncertain target

A non-SQLcl failure after the payload started clears owner_token but keeps
is_uncertain, so recover-app-lock --run-token matched zero rows and refused,
while omitting the token succeeded. The predicate now also matches a cleared
owner on an uncertain target, so naming the run being recovered is no longer
the invocation that fails.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 16: Make recovery evidence self-contained and prune `scratch/`

Two findings, one change, because the second is what makes the first safe.
Recovery records store `work_dir` pointing into `scratch/` — gitignored and named
for disposability — while the documentation insists `.sync-state/` is the durable
store. Meanwhile nothing ever deletes the four per-run SQLcl files or the
per-capture export directories, and `export-app` is the daily loop.

**Files:**
- Modify: `scripts/teamlib/apex.py` (copy evidence into the recovery record)
- Modify: `scripts/team.py` (add the `prune-scratch` command)
- Modify: `scripts/teamlib/config.py` (add `prune-scratch` to `OFFLINE_COMMANDS`)
- Create: `scripts/teamlib/prune.py`
- Modify: `README.md`, `docs/app-recovery.md`
- Test: `scripts/tests/test_prune.py` (new), `scripts/tests/test_recovery_flow.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `prune.prune_scratch(repo: Path, *, keep: int, dry_run: bool) -> dict[str, Any]`
  - `prune.main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_prune.py` with the standard preamble, then:

```python
import json
import tempfile
import unittest
from pathlib import Path

from teamlib.prune import prune_scratch


class PruneScratchTests(unittest.TestCase):
    def _repo(self, root: Path) -> Path:
        (root / "scratch").mkdir(parents=True)
        (root / ".sync-state" / "recovery").mkdir(parents=True)
        return root

    def test_old_captures_are_removed_and_the_newest_are_kept(self):
        with tempfile.TemporaryDirectory(prefix="team-prune-") as directory:
            root = self._repo(Path(directory))
            for index in range(5):
                capture = root / "scratch" / f"apex-capture-{index:032x}"
                capture.mkdir()
                (capture / "application.apx").write_text("x", encoding="utf-8")
            report = prune_scratch(root, keep=2, dry_run=False)
            remaining = sorted(p.name for p in (root / "scratch").glob("apex-capture-*"))
            self.assertEqual(len(remaining), 2)
            self.assertEqual(report["removed"], 3)

    def test_a_capture_referenced_by_a_recovery_record_is_never_removed(self):
        with tempfile.TemporaryDirectory(prefix="team-prune-") as directory:
            root = self._repo(Path(directory))
            pinned = root / "scratch" / ("apex-capture-" + "0" * 32)
            pinned.mkdir()
            for index in range(1, 5):
                (root / "scratch" / f"apex-capture-{index:032x}").mkdir()
            record = root / ".sync-state" / "recovery" / "rec-1"
            record.mkdir()
            (record / "capture.json").write_text(
                json.dumps({"work_dir": str(pinned)}), encoding="utf-8"
            )
            prune_scratch(root, keep=1, dry_run=False)
            self.assertTrue(pinned.is_dir(), "a referenced capture must survive pruning")

    def test_sqlcl_run_files_are_removed(self):
        with tempfile.TemporaryDirectory(prefix="team-prune-") as directory:
            root = self._repo(Path(directory))
            work = root / "scratch" / "metadata" / "control-abc"
            work.mkdir(parents=True)
            for name in (
                ".team-driver-abc.sql", ".team-payload-abc.sql",
                ".team-stdin-abc.empty", ".team-sqlcl-abc.log",
            ):
                (work / name).write_text("x", encoding="utf-8")
            prune_scratch(root, keep=0, dry_run=False)
            self.assertEqual(sorted(p.name for p in work.glob(".team-*")), [])

    def test_dry_run_removes_nothing(self):
        with tempfile.TemporaryDirectory(prefix="team-prune-") as directory:
            root = self._repo(Path(directory))
            (root / "scratch" / "apex-capture-aaa").mkdir()
            report = prune_scratch(root, keep=0, dry_run=True)
            self.assertTrue((root / "scratch" / "apex-capture-aaa").is_dir())
            self.assertGreater(report["would_remove"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_prune -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'teamlib.prune'`.

- [ ] **Step 3: Write the module**

Create `scripts/teamlib/prune.py`:

```python
"""Bounded retention for scratch working directories and SQLcl run files.

scratch/ holds one full application export per capture and four files per SQLcl
process, and nothing removed them. Retention is deliberate for recent evidence,
so this prunes by age while pinning anything a recovery record still points at.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
from typing import Any


class PruneError(RuntimeError):
    """Raised when scratch retention cannot be applied safely."""


_RUN_FILE_GLOBS = (".team-driver-*", ".team-payload-*", ".team-stdin-*", ".team-sqlcl-*")
_CAPTURE_GLOBS = ("apex-capture-*", "apex-import-*")


def _referenced_work_dirs(state_root: Path) -> set[str]:
    """Collect every work_dir a retained recovery record still points at."""
    referenced: set[str] = set()
    recovery = state_root / "recovery"
    if not recovery.is_dir():
        return referenced
    for path in recovery.rglob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        stack: list[Any] = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                work_dir = current.get("work_dir")
                if isinstance(work_dir, str) and work_dir:
                    referenced.add(str(Path(work_dir).resolve()))
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
    return referenced


def prune_scratch(repo: str | Path, *, keep: int = 5, dry_run: bool = False) -> dict[str, Any]:
    """Remove old capture directories and every SQLcl run file."""
    if not isinstance(keep, int) or keep < 0:
        raise PruneError("keep must be a non-negative integer")
    root = Path(repo)
    scratch = root / "scratch"
    if not scratch.is_dir():
        return {"removed": 0, "would_remove": 0, "kept": 0, "pinned": 0}
    if scratch.is_symlink():
        raise PruneError("scratch must not be a symbolic link")

    referenced = _referenced_work_dirs(root / ".sync-state")
    captures: list[Path] = []
    for pattern in _CAPTURE_GLOBS:
        captures.extend(path for path in scratch.glob(pattern) if path.is_dir() and not path.is_symlink())
    captures.sort(key=lambda path: path.stat().st_mtime, reverse=True)

    pinned = [path for path in captures if str(path.resolve()) in referenced]
    unpinned = [path for path in captures if str(path.resolve()) not in referenced]
    doomed = unpinned[keep:]

    run_files = [
        path
        for pattern in _RUN_FILE_GLOBS
        for path in scratch.rglob(pattern)
        if path.is_file() and not path.is_symlink()
    ]

    if dry_run:
        return {
            "removed": 0,
            "would_remove": len(doomed) + len(run_files),
            "kept": len(unpinned) - len(doomed),
            "pinned": len(pinned),
        }

    removed = 0
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    for path in run_files:
        path.unlink(missing_ok=True)
        removed += 1
    return {
        "removed": removed,
        "would_remove": 0,
        "kept": len(unpinned) - len(doomed),
        "pinned": len(pinned),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prune-scratch")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--keep", type=int, default=5, help="capture directories to retain")
    parser.add_argument("--dry-run", action="store_true")
    raw = list(argv or [])
    if raw and raw[0] == "prune-scratch":
        raw = raw[1:]
    args = parser.parse_args(raw)
    report = prune_scratch(args.repo, keep=args.keep, dry_run=args.dry_run)
    print(json.dumps({"status": "success", "operation": "prune-scratch", **report}, sort_keys=True))
    return 0
```

- [ ] **Step 4: Register the command**

In `scripts/teamlib/config.py`, add `"prune-scratch"` to the `OFFLINE_COMMANDS`
frozenset.

In `scripts/team.py`, add to `_offline`'s `module_names` mapping:

```python
        "prune-scratch": "prune",
```

and add `"prune-scratch"` to the `command_prefixed` set.

- [ ] **Step 5: Make recovery records self-contained**

In `scripts/teamlib/apex.py`, in `capture_app`, after `save_capture` returns a
`recovery_id` and only when `persist` is true, copy the export into the record so
the evidence survives pruning:

```python
    recovery_id = save_capture(
        target,
        {},
        {},
        head,
        tree,
        {
            "kind": "apex-capture",
            "tree_digest": tree_digest(tree),
            "before_sync": before.__dict__,
            "after_sync": after.__dict__,
            "work_dir": str(work),
        },
        root=state_root,
    ) if persist else ""
    if recovery_id:
        # scratch/ is disposable by name and by .gitignore; a recovery record
        # must not depend on it. save_capture already stores the tree bytes, so
        # this only records that the durable copy is the authoritative one.
        marker = state_root / "recovery" / recovery_id / "evidence-source.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "version": 1,
                    "authoritative": "sync-state",
                    "tree_digest": tree_digest(tree),
                    "scratch_work_dir": str(work),
                    "note": "scratch/ may be pruned; the retained tree in this record is the evidence",
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
```

- [ ] **Step 6: Document the policy**

In `README.md`, in the "Recovery and safety" section, add:

```markdown
`scratch/` is disposable working space, not evidence. Recovery records under
`.sync-state/` retain their own copy of the captured tree, so scratch can be
pruned at any time:

```text
scripts/team.sh prune-scratch --keep 5
```

Add `--dry-run` to see what would go. Capture directories a recovery record
still references are never removed.
```

In `docs/app-recovery.md`, add the same two-sentence policy near the top.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
PYTHONPATH=scripts python3 scripts/team.py prune-scratch --dry-run
```

Expected: suite OK, and the command prints a JSON report.

- [ ] **Step 8: Commit**

```bash
git add scripts/teamlib/prune.py scripts/teamlib/apex.py scripts/teamlib/config.py scripts/team.py README.md docs/app-recovery.md scripts/tests/test_prune.py
git commit -m "$(cat <<'EOF'
Bound scratch retention and stop recovery records depending on it

Nothing removed the four SQLcl run files or the per-capture export directories,
and export-app is the daily loop, so scratch grew without limit. Recovery
records also cited work_dir paths inside scratch, which is gitignored and named
disposable, so reclaiming space could destroy evidence a held application
needed. Adds prune-scratch, which pins anything a recovery record references,
and records in each capture that the retained tree is the authoritative copy.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 17: Refuse to run the control store without a working lock

Both `fcntl` and `msvcrt` are imported under `try/except ImportError` and set to
`None`. If neither is available, `_locked()` takes no lock, raises nothing, and
proceeds — read-modify-write on the JSON store with no mutual exclusion.

**Files:**
- Modify: `scripts/teamlib/control_store.py:88-100` (`ControlStore.__init__`)
- Test: `scripts/tests/test_control_store.py`

**Interfaces:**
- Consumes: nothing. Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_control_store.py`:

```python
    def test_store_refuses_to_open_without_a_locking_primitive(self):
        from teamlib import control_store
        from teamlib.control_store import ControlStore, ControlStoreError
        with tempfile.TemporaryDirectory(prefix="team-lockless-") as directory:
            with patch.object(control_store, "fcntl", None), patch.object(control_store, "msvcrt", None):
                with self.assertRaisesRegex(ControlStoreError, "advisory file locking"):
                    ControlStore(Path(directory) / "state")
```

Add `from unittest.mock import patch` to that file if absent.

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_control_store -v 2>&1 | tail -20
```

Expected: `ControlStoreError not raised`.

- [ ] **Step 3: Refuse at construction**

In `scripts/teamlib/control_store.py`, at the top of `ControlStore.__init__`:

```python
    def __init__(self, root: str | Path):
        if fcntl is None and msvcrt is None:
            # A lock that silently becomes a no-op is the wrong failure mode for
            # a store this file describes as durable.
            raise ControlStoreError(
                "control store requires advisory file locking (fcntl or msvcrt)"
            )
        self.root = Path(root)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add scripts/teamlib/control_store.py scripts/tests/test_control_store.py
git commit -m "$(cat <<'EOF'
Refuse the control store when no advisory lock is available

fcntl and msvcrt were both optional and both fell back to None, so on a
platform with neither, _locked() took no lock, raised nothing, and did a
read-modify-write on the JSON store unsynchronised. Fail at construction
instead.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 18: Tie role and environment together

`TARGET_ROLE=production` with `DB_ENVIRONMENT=development` loads cleanly, and
`run_sqlcl` gates writes on `environment` alone. The dangerous paths are
independently gated on `role`, so this is not exploitable today — it is a missing
invariant on a pair where a typo should be loud.

**Files:**
- Modify: `scripts/teamlib/config.py` (`load_config`, `parse_target_contract`)
- Test: `scripts/tests/test_config.py`

**Interfaces:**
- Consumes: nothing. Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_config.py`:

```python
class RoleEnvironmentInvariantTests(unittest.TestCase):
    def test_production_role_requires_production_environment(self):
        from teamlib.config import ConfigError, load_config
        root = Path(__file__).resolve().parents[2]
        text = (root / ".env.example").read_text(encoding="utf-8")
        mismatched = text.replace("TARGET_ROLE=developer", "TARGET_ROLE=production")
        with tempfile.TemporaryDirectory(prefix="team-config-") as directory:
            path = Path(directory) / "mismatch.env"
            path.write_text(mismatched, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "production"):
                load_config(path)

    def test_production_environment_requires_production_role(self):
        from teamlib.config import ConfigError, load_config
        root = Path(__file__).resolve().parents[2]
        text = (root / ".env.example").read_text(encoding="utf-8")
        mismatched = text.replace("DB_ENVIRONMENT=development", "DB_ENVIRONMENT=production")
        with tempfile.TemporaryDirectory(prefix="team-config-") as directory:
            path = Path(directory) / "mismatch.env"
            path.write_text(mismatched, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "production"):
                load_config(path)

    def test_the_shipped_example_and_target_contracts_still_load(self):
        from teamlib.config import load_config, parse_target_contract
        root = Path(__file__).resolve().parents[2]
        load_config(root / ".env.example")
        for name in ("development", "integration", "test", "production", "controllers", "masters"):
            path = root / "targets" / f"{name}.json"
            if path.is_file() and name not in {"controllers", "masters"}:
                parse_target_contract(path)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_config -v 2>&1 | tail -20
```

Expected: `ConfigError not raised`.

- [ ] **Step 3: Add the invariant**

In `scripts/teamlib/config.py`, in `load_config`, immediately after the
`environment` validation:

```python
    # The production classification must be unambiguous: run_sqlcl gates writes
    # on environment while the workflow commands gate on role, so a mismatched
    # pair weakens one of the two guards without any warning.
    if (role == "production") != (environment == "production"):
        raise ConfigError(
            "TARGET_ROLE and DB_ENVIRONMENT must both be production or neither"
        )
```

In `parse_target_contract`, after its `environment` validation:

```python
    if (role == "production") != (environment == "production"):
        raise ConfigError(
            "target contract role and environment must both be production or neither"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK. `targets/production.json` already declares
`"role": "production", "environment": "production"`, so no shipped contract
needs changing.

- [ ] **Step 5: Commit**

```bash
git add scripts/teamlib/config.py scripts/tests/test_config.py
git commit -m "$(cat <<'EOF'
Require role and environment to agree about production

Each field was validated against its own allowlist with nothing tying them
together, so TARGET_ROLE=production with DB_ENVIRONMENT=development loaded
cleanly and run_sqlcl, which gates on environment alone, treated it as
writable. Require both or neither.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 19: Stop scanning query results for error patterns

`_OUTPUT_ERROR_RE` matches `ORA-\d+`, `java.`, `Traceback` and similar anywhere
in stdout — including inside legitimately selected rows. A `VERIFY` script that
queries a table of Oracle error codes fails with a spurious
"SQLcl reported an error".

**Files:**
- Modify: `scripts/teamlib/sqlcl.py` (`_driver_text`, error scan at `:258`)
- Test: `scripts/tests/test_sqlcl.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the generated driver emits `TEAM_RESULT_BEGIN` / `TEAM_RESULT_END`
  markers around the payload.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_sqlcl.py`, inside `SqlclBoundaryTests`:

```python
    def test_error_scan_ignores_rows_between_the_result_markers(self):
        from teamlib.sqlcl import _diagnostic_region
        stdout = (
            "TEAM_IDENTITY|...\n"
            "TEAM_RESULT_BEGIN\n"
            "ORA-00001 unique constraint violated\n"   # a selected row, not a diagnostic
            "java.lang.String\n"
            "TEAM_RESULT_END\n"
            "TEAM_COMPLETION|operation=read\n"
        )
        self.assertNotIn("ORA-00001", _diagnostic_region(stdout))
        self.assertNotIn("java.lang.String", _diagnostic_region(stdout))

    def test_error_scan_still_sees_diagnostics_outside_the_markers(self):
        from teamlib.sqlcl import _diagnostic_region
        stdout = (
            "TEAM_RESULT_BEGIN\nrow\nTEAM_RESULT_END\n"
            "ORA-00942: table or view does not exist\n"
        )
        self.assertIn("ORA-00942", _diagnostic_region(stdout))

    def test_unbalanced_markers_fall_back_to_scanning_everything(self):
        from teamlib.sqlcl import _diagnostic_region
        stdout = "TEAM_RESULT_BEGIN\nORA-00942: boom\n"
        self.assertIn("ORA-00942", _diagnostic_region(stdout))
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_sqlcl -v 2>&1 | tail -20
```

Expected: `ImportError: cannot import name '_diagnostic_region'`.

- [ ] **Step 3: Frame the result region**

In `scripts/teamlib/sqlcl.py`, change `_driver_text` so the payload is bracketed:

```python
        + _sql_marker_query()
        + "\n"
        + "PROMPT TEAM_RESULT_BEGIN\n"
        + f"@{payload_name}\n"
        + "PROMPT TEAM_RESULT_END\n"
        + f"SELECT 'TEAM_COMPLETION|operation={operation}' FROM DUAL;\n"
        + "EXIT\n"
```

Add the helper:

```python
_RESULT_BEGIN = "TEAM_RESULT_BEGIN"
_RESULT_END = "TEAM_RESULT_END"


def _diagnostic_region(stdout: str) -> str:
    """Return output outside the payload's own result rows.

    A successful query may legitimately select text that looks like a
    diagnostic -- a table of ORA- codes, a column holding a Java class name.
    Scanning it for error patterns fails the run on its own data. Markers that
    do not pair are treated as absent, so an aborted payload is still scanned
    in full.
    """
    lines = stdout.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == _RESULT_BEGIN]
    ends = [index for index, line in enumerate(lines) if line.strip() == _RESULT_END]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return stdout
    kept = lines[: starts[0]] + lines[ends[0] + 1 :]
    return "\n".join(kept)
```

Change the error scan in `run_sqlcl`:

```python
    diagnostics = _diagnostic_region(stdout)
    if _OUTPUT_ERROR_RE.search(diagnostics) or _OUTPUT_ERROR_RE.search(stderr):
        match = _OUTPUT_ERROR_RE.search(diagnostics) or _OUTPUT_ERROR_RE.search(stderr)
        raise SqlclError(f"SQLcl reported an error ({match.group(0)}); see {log_path}")
```

Identity and completion parsing continue to read the full `stdout`, because the
markers sit outside those records.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK. If a fake-SQLcl fixture asserts on the exact driver text, update
`scripts/tests/fixtures/fake_sqlcl.py` to emit the two marker lines.

- [ ] **Step 5: Commit**

```bash
git add scripts/teamlib/sqlcl.py scripts/tests/ 
git commit -m "$(cat <<'EOF'
Scan only diagnostics, not the payload's own result rows, for errors

The error regex matched ORA- codes and java. anywhere in stdout, so a
verification query selecting a table of Oracle error codes failed the run on
its own data. Bracket the payload with result markers and scan outside them,
falling back to the whole output when the markers do not pair.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 20: Widen the owned-source allowlist to what APEX actually exports

`_read_regular_tree` refuses any suffix outside `_ALLOWED_SUFFIXES`. Deny-by-
default is right, but APEX static application files are arbitrary uploads, and
`static-files/` is not excluded — so a PDF datasheet, a `.wasm` module or a `.ts`
source makes `export-app` refuse the whole tree. A `README.md` beside the export
is rejected while extension-less `README` is accepted.

**Files:**
- Modify: `scripts/teamlib/trees.py:27-33`
- Test: `scripts/tests/test_trees.py`

**Interfaces:**
- Consumes: nothing. Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_trees.py`, inside `TreeSemanticsTests`:

```python
    def test_common_static_application_file_classes_are_accepted(self):
        with tempfile.TemporaryDirectory(prefix="team-tree-classes-") as directory:
            root = Path(directory) / "app"
            root.mkdir()
            (root / "application.apx").write_text("x", encoding="utf-8")
            for name in ("README.md", "notes.md", "module.wasm", "guide.pdf", "app.ts", "bundle.mjs"):
                (root / name).write_bytes(b"x")
            tree = read_export_tree(root)
            for name in ("README.md", "module.wasm", "guide.pdf", "app.ts", "bundle.mjs"):
                self.assertIn(name, tree)

    def test_executables_and_archives_are_still_refused(self):
        for name in ("payload.exe", "script.sh", "lib.so", "installer.msi", "bundle.tar"):
            with tempfile.TemporaryDirectory(prefix="team-tree-classes-") as directory:
                root = Path(directory) / "app"
                root.mkdir()
                (root / "application.apx").write_text("x", encoding="utf-8")
                (root / name).write_bytes(b"x")
                with self.assertRaises(TreeError, msg=name):
                    read_export_tree(root)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_trees -v 2>&1 | tail -20
```

Expected: `TreeError: unsupported exported source class: README.md`.

- [ ] **Step 3: Extend the allowlist**

In `scripts/teamlib/trees.py`, replace `_ALLOWED_SUFFIXES` with:

```python
# APEX static application files are arbitrary uploads, and static-files/ is part
# of the owned tree, so this list has to cover what a real application carries.
# It stays deny-by-default: executables, libraries and archives are excluded
# deliberately, because an application export has no reason to contain them.
_ALLOWED_SUFFIXES = {
    ".apex", ".apx", ".bin", ".css", ".csv", ".dat", ".eot", ".gif",
    ".html", ".ico", ".jpeg", ".jpg", ".js", ".json", ".map", ".md",
    ".mjs", ".mp3", ".mp4", ".otf", ".pdf", ".png", ".properties",
    ".sql", ".svg", ".ts", ".txt", ".ttf", ".wasm", ".webm", ".webp",
    ".woff", ".woff2", ".xml", ".yaml", ".yml", ".zip",
}
```

Improve the refusal message so it names the list to change:

```python
            if not _is_known_export_file(rel_path):
                raise TreeError(
                    f"unsupported exported source class: {rel_path} "
                    "(extend _ALLOWED_SUFFIXES in teamlib/trees.py if APEX legitimately exports it)"
                )
```

Apply the same message to the matching check in `read_git_tree`.

Note `.zip` was already allowed, so archives are not uniformly excluded; leave
`.zip` in place rather than changing existing behaviour, and keep `.tar`,
`.exe`, `.so`, `.sh` and `.msi` out.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add scripts/teamlib/trees.py scripts/tests/test_trees.py
git commit -m "$(cat <<'EOF'
Accept the file classes APEX static application files carry

The owned-source allowlist refused .md, .pdf, .wasm and .ts, so uploading any
of them as a static application file made export-app reject the whole tree,
while an extension-less README was fine. Keeps deny-by-default and still
refuses executables, libraries and archives, and the error now names the list
to extend.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 21: Pin the graphify patch to a known version and move it under `scripts/`

`setup_graphify_apx.py` globs for an installed `graphify` package and overwrites
two of its source files. It does roll back on failure, but the patch is invisible
to any package manager and is silently reverted by the next upgrade. It is also
the only Python file at the repository root.

**Files:**
- Move: `setup_graphify_apx.py` → `scripts/setup_graphify_apx.py`
- Modify: the moved file (version pinning)
- Modify: `README.md` if it references the old path
- Test: `scripts/tests/test_graphify_corpus.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SUPPORTED_GRAPHIFY_VERSIONS: tuple[str, ...]` in the moved module.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_graphify_corpus.py`:

```python
class GraphifyInstallerTests(unittest.TestCase):
    def test_installer_lives_with_the_rest_of_the_python_code(self):
        root = Path(__file__).resolve().parents[2]
        self.assertFalse((root / "setup_graphify_apx.py").exists())
        self.assertTrue((root / "scripts/setup_graphify_apx.py").is_file())

    def test_installer_pins_the_versions_it_knows_how_to_patch(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / "scripts/setup_graphify_apx.py").read_text(encoding="utf-8")
        self.assertIn("SUPPORTED_GRAPHIFY_VERSIONS", source)
        self.assertIn("TEAM_GRAPHIFY_ALLOW_UNTESTED", source)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_graphify_corpus -v 2>&1 | tail -20
```

Expected: `AssertionError: True is not false`.

- [ ] **Step 3: Move the file**

```bash
git mv setup_graphify_apx.py scripts/setup_graphify_apx.py
```

Update `REPO_ROOT` inside the moved file, which currently assumes it sits at the
root:

```python
REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_EXTRACTOR = REPO_ROOT / "scripts" / "graphify_apexlang_extractor.py"
```

- [ ] **Step 4: Pin the supported versions**

Add near the top of the moved file:

```python
# Patching another package's installed source is reverted silently by the next
# upgrade of that package. Record what this installer was written against and
# refuse anything else, so an upgrade surfaces as a clear message rather than
# .apx support quietly disappearing.
SUPPORTED_GRAPHIFY_VERSIONS = ("0.1", "0.2", "0.3")


def _graphify_version(base):
    for name in ("__version__.py", "version.py", "__init__.py"):
        candidate = Path(base) / name
        if not candidate.is_file():
            continue
        match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", candidate.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
    return None


def _version_is_supported(base):
    if os.environ.get("TEAM_GRAPHIFY_ALLOW_UNTESTED") == "1":
        return True, "override"
    version = _graphify_version(base)
    if version is None:
        return False, "the installed Graphify version could not be determined"
    if not any(version.startswith(supported) for supported in SUPPORTED_GRAPHIFY_VERSIONS):
        return False, f"Graphify {version} is not one of {', '.join(SUPPORTED_GRAPHIFY_VERSIONS)}"
    return True, version
```

Add `import re` to that file's imports if absent, and call the check at the top
of the function that performs the patch, immediately before the first
`write_text`:

```python
    supported, detail = _version_is_supported(base)
    if not supported:
        print(
            f"Warning: refusing to patch Graphify at '{base}': {detail}. "
            "Set TEAM_GRAPHIFY_ALLOW_UNTESTED=1 to proceed anyway."
        )
        return False
```

- [ ] **Step 5: Update any references to the old path**

```bash
grep -rn 'setup_graphify_apx' --include='*.md' --include='*.yml' --include='*.py' . | grep -v '.git/'
```

Update every hit outside `scripts/` to `scripts/setup_graphify_apx.py`.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
python3 -m ruff check scripts/
```

Expected: OK and `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Pin the Graphify patch to known versions and move it under scripts

The installer overwrites two files inside an installed third-party package, so
the next upgrade of that package silently reverts .apx support. Record the
versions it was written against and refuse anything else unless explicitly
overridden. Also moves the only Python file at the repository root in with the
rest of the code.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 22: Narrow the top-level exception handler and quiet the test suite

`team.py:566` catches bare `ValueError`. `ConfigError` and `TreeError` already
subclass it and are caught by name, so the extra clause only swallows incidental
`ValueError`s from real bugs, reporting them as a clean exit-3 with no traceback.
Three tests also print `PAUSE:` lines and raw JSON to stdout.

**Files:**
- Modify: `scripts/team.py` (exception tuple)
- Modify: `scripts/teamlib/apex.py:455` (route the pause notice through a callback)
- Test: `scripts/tests/test_launchers.py`, `scripts/tests/test_import_app.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `import_app(..., announce: Callable[[str], None] = print)`.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_launchers.py`:

```python
class ExceptionSurfaceTests(unittest.TestCase):
    def test_incidental_value_errors_are_not_swallowed_as_a_clean_exit(self):
        def explode(argv=None):
            raise ValueError("an incidental bug, not a domain refusal")

        with patch("teamlib.ci.main", explode):
            with self.assertRaises(ValueError):
                team.main(["ci-doctor", "--contract", "ci/runner-contract.json"])

    def test_domain_refusals_still_exit_with_the_documented_codes(self):
        from teamlib.config import ConfigError

        def refuse(argv=None):
            raise ConfigError("a domain refusal")

        with patch("teamlib.ci.main", refuse):
            self.assertEqual(team.main(["ci-doctor", "--contract", "ci/runner-contract.json"]), 2)
```

Append to `scripts/tests/test_import_app.py`:

```python
    def test_pause_notice_goes_to_the_supplied_announcer_not_stdout(self):
        notices: list[str] = []
        # Reuse this file's existing successful-import fixture, adding the
        # announce callback; the assertion is that stdout stays clean.
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self._run_successful_import(announce=notices.append)
        self.assertEqual(buffer.getvalue(), "")
        self.assertTrue(any("PAUSE:" in notice for notice in notices))
```

If `test_import_app.py` has no `_run_successful_import` helper, extract one from
its existing successful-import test and have both call it.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=scripts python3 -m unittest scripts.tests.test_launchers scripts.tests.test_import_app -v 2>&1 | tail -20
```

Expected: the `ValueError` test fails because `main` returns 3 instead of
raising.

- [ ] **Step 3: Narrow the handler**

In `scripts/team.py`, remove `ValueError` from the caught tuple:

```python
    except (ConfigError, ControlStoreError, StateError, PatchError, ApexError, MigrationRunError, MigrationStoreError, DeployError, ReleaseError, RunbookError, CIError, AppCheckError, TreeError, BundleError) as exc:
        print(str(exc), file=sys.stderr)
        return 2 if isinstance(exc, ConfigError) else 3
```

Add the two imports that were previously reached implicitly through `ValueError`:

```python
from teamlib.migration_bundle import BundleError
```

(`TreeError` is already imported.)

- [ ] **Step 4: Route the pause notice through a callback**

In `scripts/teamlib/apex.py`, add the parameter to `import_app`:

```python
    announce: Callable[[str], None] = print,
```

and replace the bare `print(...)` with:

```python
        announce(
            f"PAUSE: importing {target.alias} application {target.app_id} into "
            f"{target.instance_id}/workspace {target.workspace_id}; "
            "all Builder edits must stop until verification completes."
        )
```

`scripts/team.py` keeps the default, so the operator still sees the notice.

- [ ] **Step 5: Verify the suite is quiet**

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests 2>&1 | grep -E 'PAUSE:|^\{' | head
```

Expected: no output.

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
```

Expected: OK.

- [ ] **Step 6: Commit**

```bash
git add scripts/team.py scripts/teamlib/apex.py scripts/tests/
git commit -m "$(cat <<'EOF'
Stop swallowing incidental ValueErrors and quiet the test suite

ConfigError and TreeError already subclass ValueError and were caught by name,
so the extra clause only hid genuine bugs behind a terse exit-3 with no
traceback. The import pause notice now goes through an injectable announcer, so
tests no longer print PAUSE lines and raw JSON to stdout.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

# Final verification

- [ ] **Run every gate the CI runs**

```bash
for script in scripts/*.sh; do bash -n "$script"; done
python3 -m ruff check scripts/
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v 2>&1 | tail -5
PYTHONPATH=scripts python3 scripts/team.py ci-doctor --contract ci/runner-contract.json
find targets -name '*.json' -print0 | xargs -0 -n1 python3 -m json.tool >/dev/null && echo "targets json ok"
! grep -RIl $'\r' --include='*.apx' --include='*.sql' --include='*.json' --include='*.sh' --include='*.ps1' . && echo "line endings ok"
```

Expected: shell syntax silent, ruff `All checks passed!`, unittest `OK` with at
least 205 tests, ci-doctor valid, both trailing checks printing `ok`.

- [ ] **Confirm every reported reproduction is closed**

```bash
PYTHONPATH=scripts python3 - <<'PY'
from teamlib.release_adapter import main as apply_main
from teamlib.sqlcl import _assert_production_read_only as chk, SqlclError
from teamlib.migration_bundle import _mask_code, _assert_controls
from teamlib.config import load_config, ConfigError
from teamlib.sql_text import clob_builder
import json, pathlib

checks = []
try:
    apply_main(["apply-release", "x.tar", "--plan", "p.json", "--history", "h.json", "--target", "targets/test.json"])
except SystemExit:
    checks.append(("1 apply-release NameError", "fixed"))
except NameError:
    checks.append(("1 apply-release NameError", "STILL BROKEN"))

_mask_code("UPDATE t SET m = q'[don't touch]' WHERE id=1;")
checks.append(("4 q-quote bundle", "fixed"))
chk("SELECT q'[don't drop this table]' FROM dual;")
checks.append(("4 q-quote guard", "fixed"))
_assert_controls("SELECT id FROM org\nSTART WITH id=1\nCONNECT BY PRIOR id=p;\n")
_assert_controls(pathlib.Path("scripts/sql/control_metadata.sql").read_text(encoding="utf-8"))
checks.append(("5 CONNECT BY / host column", "fixed"))
for bad in ["HOST rm -rf /tmp/x", "!id", "SCRIPT var x=1;", "@/tmp/o.sql", "SPOOL /tmp/o", "CALL p();"]:
    try:
        chk(bad); checks.append((f"8 guard {bad!r}", "STILL PASSES")); break
    except SqlclError:
        pass
else:
    checks.append(("8 production allowlist", "fixed"))
m = json.dumps({"o": [{"n": f"T{i}", "h": "a"*64} for i in range(400)]}, separators=(",", ":"))
checks.append(("9 clob max line", str(max(len(l) for l in clob_builder("v", m).splitlines()))))
for name, verdict in checks:
    print(f"  {name:34s} {verdict}")
PY
```

Expected: every row reads `fixed`, and the CLOB max line is near 1030.

- [ ] **Confirm the tree is clean and the history is coherent**

```bash
git status --porcelain=v1 --untracked-files=all && echo "(clean)"
git log --oneline main..HEAD
```

Expected: a clean tree and 22 commits, one per task.

---

## Self-review notes

**Coverage.** All 27 review findings map to a task: Task 1 (finding B1), 2 (B2),
3 (health: dead imports, `Any`, `__import__`; minor: redundant `import io`;
health: no linter), 4 (health: orphan module; minor: `clob=True`, no-op ternary,
arrow branch), 5-6 (B4, health: private cross-module imports), 7 (B5), 8
(hardening: production denylist), 9 (B6), 10 (B7), 11 (B3), 12 (B8, health:
cryptography masking), 13 (ops: CI Python versions), 14 (ops: `Path.cwd()`),
15 (ops: recover-app-lock), 16 (ops: scratch growth, evidence in scratch), 17
(ops: lock no-op), 18 (hardening: role/environment), 19 (hardening:
`_OUTPUT_ERROR_RE`), 20 (hardening: trees allowlist), 21 (hardening: graphify),
22 (health: bare `ValueError`; minor: test stdout).

**Not addressed, deliberately.** The `apex.py:146` precedence expression is
correct as written and reads ambiguously only to a reader unfamiliar with
Python's `and`/`or` binding; changing it carries risk without behavioural
benefit. The missing directory `fsync` after `os.replace` in the two state stores
is a real durability gap but needs its own design discussion about which
platforms to support, so it is not folded into a remediation pass.

**Ordering dependency.** Task 4 must precede Task 5, because Task 5 moves
`_b64_sql` and Task 4 changes its signature. Task 6 must follow Task 5. Task 9
must follow Task 6, which introduces `_clob_builder`. Task 12 must follow Task 3,
which creates `pyproject.toml`. Every other task is independent.
