# Guard and Workflow Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every verified defect from the 2026-09-10 review — starting with three statement-boundary bypasses that let refused SQL through the production and migration guards — without loosening any safety boundary.

**Architecture:** Five phases ordered by blast radius. Phase 1 replaces the two hand-written line-oriented statement walkers with one Oracle-aware statement splitter in `sql_text`, which is the single root cause of the guard bypasses and of the production-read guard that refuses the repository's own inventory SQL. Phase 2 repairs command paths that always fail in a real environment. Phase 3 makes the shipped CI reachable end to end. Phase 4 tightens error contracts so a bad input produces a refusal, not a traceback. Phase 5 bounds generated working directories and removes dead code. Every guard change makes the guard **stricter or more precise, never more permissive**, except the one place where it currently rejects valid Oracle SQL (Task 4).

**Tech Stack:** Python 3.10+ (stdlib only for the core workflow), `unittest`, qualified SQLcl/Oracle, thin Bash/PowerShell launchers, GitHub Actions.

**Spec:** [Team design](../specs/2026-09-06-team-template-design.md) — the safety contract this plan must not weaken. The prior round is [2026-09-09-review-remediation.md](2026-09-09-review-remediation.md); its Tasks 1–20 are already merged (commits `eb6350b`..`1582600`) and are not repeated here.

---

## Global Constraints

- **Python floor:** `requires-python = ">=3.10"`. Every CI job pins `python-version: '3.10'`. No syntax or stdlib API newer than 3.10.
- **No new runtime dependencies.** The daily workflow (export, reconcile, migrate, release) is stdlib-only. `cryptography` stays confined to the `promotion` extra; `graphify`/`tree-sitter-sql` stay confined to the `graph` extra.
- **Lint gate:** `python3 -m ruff check scripts/` must pass. Selected rules are `F`, `E9`, `B`, `UP`; line length 200; target `py310`.
- **Full suite:** `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -q` must pass (235 tests green at plan time). No task may leave it red.
- **Guards fail closed.** When a change alters what a guard accepts, the accompanying test must assert both the newly-refused case and that a previously-valid case still passes.
- **Production writes stay refused.** No task may add a write path for `environment == "production"`.
- **Line endings:** every file written by the tooling uses `newline="\n"`. `template-checks.yml` fails on any CR in `*.apx`, `*.sql`, `*.json`, `*.sh`, `*.ps1`.
- **Commit style:** imperative subject describing the behaviour change, no scope prefix invented for this repo — match the existing log (e.g. `Stop swallowing incidental ValueErrors and quiet the import-pause print`).

---

## Findings Traceability

Two claims from the incoming report were checked and do **not** hold as stated; they are carried at reduced severity with corrected framing:

- **`save_capture`/`load_capture` mismatch** is latent, not live. Every real caller (`apex.py:202`, `apex.py:274`, `deploy.py:87`) supplies either a commit string or a `head_commit` diagnostic, so the rejected shape is unreachable today. Handled in Task 16.
- **`ci_replay_runner` app checks** do not break the template's own CI: `apps/` holds only `.gitkeep`, so `aliases` is empty and the zero-app coverage stub is returned, exactly as `ci/app-checks/README.md` documents. It breaks the *first adopting team* that adds an application. Handled in Tasks 9 and 10.

| # | Defect | Task |
|---|---|---|
| 1 | Inline `;` bypasses the production read-only allowlist | 1, 2, 4 |
| 2 | Inline `;` bypasses the migration client-command check | 1, 2, 3 |
| 3 | Unterminated client command swallows every following line | 2, 3, 4 |
| 4 | `q'[...]'` literal forges a `depends-on` directive | 1, 3 |
| 5 | Production read guard refuses `schema_inventory.sql` | 4 |
| 6 | `apply-release` bootstraps `schema_set_digest="release"` | 5 |
| 7 | `deploy-app` runs APEX import/export on the 120 s budget | 6 |
| 8 | `assert_source_clean` refuses an ignored `export.log` | 7 |
| 9 | `release.yml` never installs `cryptography` | 8 |
| 10 | No `select_runner` for candidate app checks | 9 |
| 11 | No named refusal when a flow runner is absent | 10 |
| 12 | `check-drift` cannot pass before the first migration | 11 |
| 13 | `SqlControlStore.acquire_app` returns `None` | 12 |
| 14 | Non-string `binding.connection` raises `TypeError` | 13 |
| 15 | Offline handlers raise uncaught `OSError`/`InventoryError` | 14 |
| 16 | Inventory manifest CLOB chunked as if ASCII | 15 |
| 17 | `save_capture` accepts a shape `load_capture` rejects | 16 |
| 18 | `is_offline_command` prefix branch dead and broken | 16 |
| 19 | `prune-scratch` misses deploy and metadata directories | 17 |
| 20 | Hygiene: dead import, unused param, no-op branch, launcher drift | 18 |

---

## File Structure

**Created:**
- `scripts/tests/test_statement_starts.py` — unit tests for the new shared statement splitter and comment scanner.

**Modified:**
- `scripts/teamlib/sql_text.py` — gains `comment_spans`, `statement_starts`, `BLOCK_START_RE`; `mask_sql` is rebuilt on a single shared token scan. This is the one module that understands Oracle lexical structure.
- `scripts/teamlib/migration_bundle.py` — `_comment_directives` and `_statement_leading_lines` deleted; both callers move to `sql_text`.
- `scripts/teamlib/sqlcl.py` — production read-only guard moves to `statement_starts` and gains an explicit allowance for a read-only `DBMS_METADATA` session-setup block.
- `scripts/teamlib/release_adapter.py`, `scripts/teamlib/deploy.py`, `scripts/teamlib/trees.py`, `scripts/teamlib/control_store.py`, `scripts/teamlib/config.py`, `scripts/teamlib/fingerprints.py`, `scripts/teamlib/migration_store.py`, `scripts/teamlib/state.py`, `scripts/teamlib/prune.py`, `scripts/teamlib/app_checks.py`, `scripts/teamlib/live_inventory.py`, `scripts/teamlib/release.py` — one defect each.
- `scripts/team.py` — new `adopt-frontier` command; offline handler error containment.
- `scripts/ci_replay_runner.py` — real `select_runner`, pluggable `flow_runner`.
- `.github/workflows/release.yml`, `.github/workflows/integration.yml` — dependency install, command ordering.
- `scripts/build_release.ps1`, `.env.example`, `docs/ci.md`, `docs/schema-coverage.md` — consistency and documentation.
- Test files alongside each change.

---

# Phase 1 — Statement-boundary correctness

Three separate defects share one cause: two modules walk SQL **lines** and decide "does a statement start here?" from whether the previous line ended in `;`. That model misses a second statement after an inline `;`, and it silently swallows every line after a client command that has no `;` at all. Phase 1 replaces both walkers with one splitter.

Evidence to keep in mind while implementing — this is today's behaviour on `scripts/sql/identity.sql`:

```text
statement starts found: line 4 only ("SET DEFINE OFF")
lines 5, 6 (SET HEADING OFF, SET FEEDBACK OFF) and the SELECT on line 7
are never offered to any guard, because line 4 has no trailing ";"
```

---

## Task 1: Give `sql_text` one token scan and expose comment spans

**Files:**
- Modify: `scripts/teamlib/sql_text.py:108-191` (replace `mask_sql`, add `_scan` and `comment_spans`)
- Test: `scripts/tests/test_sql_text.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `sql_text._scan(text: str) -> tuple[list[tuple[str, int, int]], bool]` — private. Returns `(spans, terminated)` where each span is `(kind, start, stop)` with `kind` in `{"line-comment", "block-comment", "literal"}`, `start` inclusive, `stop` exclusive, spans in ascending order and non-overlapping.
  - `sql_text.mask_sql(text: str) -> tuple[str, bool]` — unchanged signature and behaviour, now built on `_scan`.
  - `sql_text.comment_spans(text: str) -> tuple[tuple[int, int], ...]` — offsets of every real `--` line comment, q-quote aware.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_sql_text.py`:

```python
class CommentSpanTests(unittest.TestCase):
    def test_line_comment_offsets_are_reported(self):
        from teamlib.sql_text import comment_spans

        text = "SELECT 1 FROM dual; -- trailing note\nSELECT 2 FROM dual;\n"
        self.assertEqual(comment_spans(text), ((20, 36),))
        self.assertEqual(text[20:36], "-- trailing note")

    def test_q_quoted_literal_hides_a_comment_marker(self):
        from teamlib.sql_text import comment_spans

        # The apostrophe in "It's" must not end the q-quoted literal, so the
        # "--" inside it is data, not a comment.
        text = "SELECT q'[It's a trap -- depends-on: forged]' FROM dual;\n"
        self.assertEqual(comment_spans(text), ())

    def test_block_comment_is_not_a_line_comment(self):
        from teamlib.sql_text import comment_spans

        self.assertEqual(comment_spans("/* -- not a line comment */\n"), ())

    def test_mask_sql_still_blanks_the_same_text(self):
        from teamlib.sql_text import mask_sql

        masked, terminated = mask_sql("SELECT 'abc' FROM dual; -- note\n")
        self.assertTrue(terminated)
        self.assertEqual(masked, "SELECT       FROM dual;       \n")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=scripts python3 -m unittest scripts.tests.test_sql_text -v 2>&1 | tail -20`

Note: the suite is discovered with `-s scripts/tests -t scripts`, so the importable module path is `tests.test_sql_text`. Use:

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_sql_text -v 2>&1 | tail -20`
Expected: FAIL with `ImportError: cannot import name 'comment_spans' from 'teamlib.sql_text'`

- [ ] **Step 3: Replace `mask_sql` with a scan-based implementation**

In `scripts/teamlib/sql_text.py`, replace the whole `_mask_span` + `mask_sql` block (from `def _mask_span` to end of file) with:

```python
def _scan(text: str) -> tuple[list[tuple[str, int, int]], bool]:
    """Locate every comment and literal span, Oracle alternative quoting included.

    Returns ``(spans, terminated)``. ``terminated`` is False when a construct
    runs off the end of the text, which every caller treats as a refusal: an
    unterminated literal means the rest of the file is not what it looks like.
    Spans are ascending, non-overlapping, and ``stop`` is exclusive.
    """
    spans: list[tuple[str, int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        current = text[index]
        following = text[index + 1] if index + 1 < length else ""

        if current == "-" and following == "-":
            end = text.find("\n", index)
            if end == -1:
                end = length
            spans.append(("line-comment", index, end))
            index = end
            continue

        if current == "/" and following == "*":
            end = text.find("*/", index + 2)
            if end == -1:
                spans.append(("block-comment", index, length))
                return spans, False
            spans.append(("block-comment", index, end + 2))
            index = end + 2
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
                    spans.append(("literal", start, length))
                    return spans, False
                spans.append(("literal", start, end + 2))
                index = end + 2
                continue

        if current in {"'", '"'}:
            quote = current
            cursor = index + 1
            while cursor < length:
                if text[cursor] == quote:
                    if cursor + 1 < length and text[cursor + 1] == quote:
                        cursor += 2
                        continue
                    spans.append(("literal", index, cursor + 1))
                    index = cursor + 1
                    break
                cursor += 1
            else:
                spans.append(("literal", index, length))
                return spans, False
            continue

        index += 1
    return spans, True


def mask_sql(text: str) -> tuple[str, bool]:
    """Blank comments and literals, preserving offsets and newlines.

    Returns the masked text and whether every construct was terminated. Oracle
    alternative quoting (``q'[...]'``, ``nq'{...}'``) is recognised, so an
    embedded apostrophe neither ends the literal early nor leaks the rest of
    the literal's text into the masked output as if it were code.
    """
    spans, terminated = _scan(text)
    chars = list(text)
    for _, start, stop in spans:
        for index in range(start, stop):
            if text[index] != "\n":
                chars[index] = " "
    return "".join(chars), terminated


def comment_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return ``(start, stop)`` offsets of every real ``--`` line comment.

    A ``--`` inside a string literal -- including a q-quoted literal whose body
    contains an apostrophe -- is data and is not reported. Callers that parse
    directives out of comments must use this rather than scanning raw lines.
    """
    spans, terminated = _scan(text)
    if not terminated:
        raise SqlTextError("unterminated SQL comment or literal")
    return tuple((start, stop) for kind, start, stop in spans if kind == "line-comment")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_sql_text -v 2>&1 | tail -20`
Expected: PASS, all tests in the module

- [ ] **Step 5: Run the full suite — `mask_sql` has many callers**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5`
Expected: `OK`, 235+ tests

- [ ] **Step 6: Lint**

Run: `python3 -m ruff check scripts/`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add scripts/teamlib/sql_text.py scripts/tests/test_sql_text.py
git commit -m "Build mask_sql on one token scan and expose comment spans"
```

---

## Task 2: Add a statement splitter that honours inline semicolons

**Files:**
- Modify: `scripts/teamlib/sql_text.py` (add `BLOCK_START_RE`, `_LINE_TERMINATED_RE`, `statement_starts`)
- Create: `scripts/tests/test_statement_starts.py`

**Interfaces:**
- Consumes: `sql_text.mask_sql` (Task 1).
- Produces:
  - `sql_text.BLOCK_START_RE: re.Pattern` — matches the leading keyword of a PL/SQL block (`DECLARE`, `BEGIN`, `CREATE [OR REPLACE] PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE`).
  - `sql_text.statement_starts(masked: str) -> Iterator[tuple[int, str]]` — yields `(line_number, text)` once per top-level statement. `line_number` is 1-based. `text` runs from the statement's first non-blank character to the next top-level `;` or the end of that line, stripped.

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_statement_starts.py`:

```python
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import unittest

from teamlib.sql_text import mask_sql, statement_starts


def starts(text: str) -> list[tuple[int, str]]:
    masked, terminated = mask_sql(text)
    assert terminated, "fixture must be lexically complete"
    return list(statement_starts(masked))


class StatementStartTests(unittest.TestCase):
    def test_second_statement_on_one_line_is_offered(self):
        self.assertEqual(
            starts("SELECT 1 FROM dual; DROP TABLE audit_log;\n"),
            [(1, "SELECT 1 FROM dual"), (1, "DROP TABLE audit_log")],
        )

    def test_client_command_is_terminated_by_end_of_line(self):
        # SET has no ";". Every following line must still be offered.
        self.assertEqual(
            starts("SET DEFINE OFF\nSET HEADING OFF\nSELECT 1 FROM dual;\n"),
            [(1, "SET DEFINE OFF"), (2, "SET HEADING OFF"), (3, "SELECT 1 FROM dual")],
        )

    def test_continuation_line_of_a_query_is_not_a_statement_start(self):
        self.assertEqual(
            starts("SELECT a\nFROM t\nWHERE b = 1;\n"),
            [(1, "SELECT a")],
        )

    def test_plsql_block_is_one_statement_ending_at_a_lone_slash(self):
        text = (
            "BEGIN\n"
            "  DBMS_OUTPUT.PUT_LINE('x');\n"
            "  HOST rm -rf /;\n"
            "END;\n"
            "/\n"
            "SELECT 1 FROM dual;\n"
        )
        # Nothing inside the block is a statement start; the block itself is.
        self.assertEqual(starts(text), [(1, "BEGIN"), (6, "SELECT 1 FROM dual")])

    def test_semicolon_inside_a_literal_does_not_split(self):
        self.assertEqual(
            starts("SELECT 'a; DROP TABLE t' FROM dual;\n"),
            [(1, "SELECT")],
        )

    def test_slash_terminates_a_statement_outside_a_block(self):
        self.assertEqual(
            starts("SELECT 1 FROM dual\n/\nSELECT 2 FROM dual;\n"),
            [(1, "SELECT 1 FROM dual"), (3, "SELECT 2 FROM dual")],
        )

    def test_create_table_is_not_a_plsql_block(self):
        self.assertEqual(
            starts("CREATE TABLE t (a NUMBER);\nSELECT 1 FROM dual;\n"),
            [(1, "CREATE TABLE t (a NUMBER)"), (2, "SELECT 1 FROM dual")],
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_statement_starts -v 2>&1 | tail -20`
Expected: FAIL with `ImportError: cannot import name 'statement_starts' from 'teamlib.sql_text'`

- [ ] **Step 3: Implement the splitter**

Append to `scripts/teamlib/sql_text.py`:

```python
# A PL/SQL block is one statement whose body may contain any number of
# semicolons. It ends at a line containing only "/", never at a ";".
BLOCK_START_RE = re.compile(
    r"^(?:DECLARE|BEGIN)\b"
    r"|^CREATE(?:\s+OR\s+REPLACE)?(?:\s+(?:EDITIONABLE|NONEDITIONABLE))?\s+"
    r"(?:PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE)\b",
    re.IGNORECASE,
)
# SQLcl client commands are terminated by the end of the line, not by ";".
# Treating them as SQL made one unterminated "SET DEFINE OFF" swallow every
# following statement, so nothing after it was ever offered to a guard.
_LINE_TERMINATED_RE = re.compile(
    r"^(?:@{1,2}|!)"
    r"|^(?:SET|SHOW|SPOOL|PROMPT|WHENEVER|EXIT|QUIT|CONNECT|CONN|DISCONNECT|DISC"
    r"|HOST|HO|START|SCRIPT|DEFINE|DEF|UNDEFINE|UNDEF|COLUMN|COL|CLEAR|ACCEPT|ACC"
    r"|PAUSE|TIMING|REM|REMARK|DESCRIBE|DESC|EXECUTE|EXEC|ALIAS|APEX|LIQUIBASE|LB"
    r"|CD|INFO|HISTORY)\b",
    re.IGNORECASE,
)


def statement_starts(masked: str):
    """Yield ``(line_number, text)`` for every top-level statement.

    ``masked`` must be :func:`mask_sql` output, so a ``;`` it still contains
    really does terminate a statement rather than sitting inside a literal or a
    comment. ``text`` runs from the statement's first non-blank character to the
    next top-level ``;`` or the end of that line -- enough to match a leading
    keyword, which is all any guard in this repository inspects.

    Two properties matter and are why this replaced the per-line walkers:
    a second statement after an inline ``;`` is offered, and a client command
    with no ``;`` does not hide the lines that follow it.
    """
    pending = True
    in_block = False
    for number, raw in enumerate(masked.splitlines(), start=1):
        line = raw.strip()
        if in_block:
            if line == "/":
                in_block = False
                pending = True
            continue
        if not line:
            continue
        if line == "/":
            pending = True
            continue
        position = 0
        line_length = len(line)
        while position < line_length:
            semicolon = line.find(";", position)
            piece = (line[position:semicolon] if semicolon >= 0 else line[position:]).strip()
            if pending and piece:
                yield number, piece
                if BLOCK_START_RE.match(piece):
                    in_block = True
                    pending = False
                    break
                if _LINE_TERMINATED_RE.match(piece):
                    # The command ended with this line regardless of any ";".
                    pending = True
                    break
            if semicolon < 0:
                pending = False
                break
            pending = True
            position = semicolon + 1
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_statement_starts -v 2>&1 | tail -20`
Expected: PASS, 7 tests

- [ ] **Step 5: Check the splitter against every SQL file the repository ships**

Run:

```bash
cd /home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM && PYTHONPATH=scripts python3 -c "
from pathlib import Path
from teamlib.sql_text import mask_sql, statement_starts
for path in sorted(Path('scripts/sql').glob('*.sql')):
    masked, ok = mask_sql(path.read_text(encoding='utf-8'))
    print(f'--- {path.name} terminated={ok}')
    for number, text in statement_starts(masked):
        print(f'   {number}: {text[:60]}')
"
```

Expected: `identity.sql` now reports **four** statements (lines 4, 5, 6, 7 — `SET DEFINE OFF`, `SET HEADING OFF`, `SET FEEDBACK OFF`, `SELECT ...`) where the old walker reported one. `schema_inventory.sql` reports `BEGIN` at line 4 plus the three `WITH` statements and the trailing `SELECT`. `control_metadata.sql` and `migration_metadata.sql` report one `CREATE TABLE` per table rather than one for the whole file.

- [ ] **Step 6: Run the full suite and lint**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5 && python3 -m ruff check scripts/`
Expected: `OK` then `All checks passed!` (nothing consumes `statement_starts` yet)

- [ ] **Step 7: Commit**

```bash
git add scripts/teamlib/sql_text.py scripts/tests/test_statement_starts.py
git commit -m "Split SQL into statements instead of walking lines"
```

---

## Task 3: Move the migration guards onto the shared lexer

**Files:**
- Modify: `scripts/teamlib/migration_bundle.py:64-113` (delete `_comment_directives` body, rewrite on `comment_spans`)
- Modify: `scripts/teamlib/migration_bundle.py:162-202` (delete `_BLOCK_START_RE` and `_statement_leading_lines`)
- Modify: `scripts/teamlib/migration_bundle.py:204-218` (`_assert_controls` consumes `statement_starts`)
- Test: `scripts/tests/test_migration_bundle.py` — if that module does not exist, add the tests to `scripts/tests/test_patch.py`'s sibling `scripts/tests/test_migration_plan.py`; run `ls scripts/tests/` first and place them in the file that already imports from `teamlib.migration_bundle`.

**Interfaces:**
- Consumes: `sql_text.comment_spans`, `sql_text.statement_starts`, `sql_text.mask_sql` (Tasks 1–2).
- Produces: no signature changes. `_assert_controls(text, *, verify=False) -> None` and `_comment_directives(text) -> list[tuple[int, str, str]]` keep their contracts; only their accuracy changes.

- [ ] **Step 1: Locate the test module**

Run: `ls scripts/tests/ | grep -i -E "bundle|migration"`
Expected: the file list; use the module that already imports `teamlib.migration_bundle`. Confirm with:

Run: `grep -ln "migration_bundle" scripts/tests/*.py`

- [ ] **Step 2: Write the failing tests**

Append to that module (it already has the imports for `unittest` and the `sys.path` preamble):

```python
class StatementBoundaryGuardTests(unittest.TestCase):
    def test_inline_client_command_after_a_query_is_refused(self):
        from teamlib.migration_bundle import BundleError, _assert_controls

        with self.assertRaises(BundleError) as raised:
            _assert_controls("SELECT 1 FROM dual; HOST rm -rf /;\n")
        self.assertIn("control command", str(raised.exception))

    def test_inline_nested_include_after_a_query_is_refused(self):
        from teamlib.migration_bundle import BundleError, _assert_controls

        with self.assertRaises(BundleError) as raised:
            _assert_controls("SELECT 1 FROM dual; @malicious.sql;\n")
        self.assertIn("nested SQLcl includes", str(raised.exception))

    def test_command_after_an_unterminated_set_is_refused(self):
        from teamlib.migration_bundle import BundleError, _assert_controls

        with self.assertRaises(BundleError):
            _assert_controls("SET HEADING OFF\nHOST rm -rf /;\n")

    def test_ordinary_sql_using_reserved_words_still_passes(self):
        from teamlib.migration_bundle import _assert_controls

        _assert_controls(
            "SELECT level\n"
            "  FROM dual\n"
            "CONNECT BY level <= 3;\n"
            "CREATE TABLE t (host VARCHAR2(30));\n"
        )

    def test_directive_inside_a_q_quoted_literal_is_not_a_directive(self):
        from teamlib.migration_bundle import _comment_directives

        text = "SELECT q'[It's a trap -- depends-on: forged]' FROM dual;\n"
        self.assertEqual(_comment_directives(text), [])

    def test_real_directives_are_still_parsed(self):
        from teamlib.migration_bundle import _comment_directives

        text = "-- migration-version: 1\n-- target: tables\nSELECT 1 FROM dual;\n"
        self.assertEqual(
            _comment_directives(text),
            [(0, "migration-version", "1"), (24, "target", "tables")],
        )
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `PYTHONPATH=scripts python3 -m unittest tests.<module> -k StatementBoundaryGuard -v 2>&1 | tail -25`

(substitute the module name found in Step 1)

Expected: FAIL — `test_inline_client_command_after_a_query_is_refused`, `test_inline_nested_include_after_a_query_is_refused`, `test_command_after_an_unterminated_set_is_refused` and `test_directive_inside_a_q_quoted_literal_is_not_a_directive` all fail because nothing is raised / a forged directive is returned.

- [ ] **Step 4: Rewrite `_comment_directives` on `comment_spans`**

In `scripts/teamlib/migration_bundle.py`, replace the entire `_comment_directives` function (the hand-written scanner, lines 64–113) with:

```python
def _comment_directives(text: str) -> list[tuple[int, str, str]]:
    """Return real line-comment directives, not strings or block comments.

    Comment locations come from the shared Oracle-aware scanner, so an
    apostrophe inside a ``q'[...]'`` literal can no longer end string state
    early and turn the literal's own text into a directive.
    """
    result: list[tuple[int, str, str]] = []
    try:
        spans = comment_spans(text)
    except SqlTextError as exc:
        raise BundleError(str(exc)) from exc
    for start, stop in spans:
        match = _DIRECTIVE_RE.match(text[start:stop])
        if match:
            result.append((start, match.group(1).casefold(), match.group(2)))
    return result
```

Update the import at the top of the module from:

```python
from .sql_text import mask_sql
```

to:

```python
from .sql_text import SqlTextError, comment_spans, mask_sql, statement_starts
```

`BLOCK_START_RE` is deliberately **not** imported: after Step 5 nothing in this module needs it, and ruff's `F401` would flag it. Block detection now lives entirely inside `statement_starts`.

- [ ] **Step 5: Delete the local walker and rewire `_assert_controls`**

Delete `_BLOCK_START_RE` and the whole `_statement_leading_lines` function. Replace `_assert_controls` with:

```python
def _assert_controls(text: str, *, verify: bool = False) -> None:
    code = _mask_code(text)
    for number, statement in statement_starts(code):
        if re.match(r"^(?:@{1,2}|START\b|SCRIPT\b)", statement, re.IGNORECASE):
            raise BundleError(f"nested SQLcl includes are prohibited (line {number})")
        if _CLIENT_COMMAND_RE.match(statement):
            raise BundleError(
                f"SQLcl control command is prohibited in migration members (line {number})"
            )
    if re.search(r"(?i)\bSET\s+(?:DEFINE\s+ON|SQLTERMINATOR\s+OFF|ESCAPE\s+ON)\b", code):
        raise BundleError("SQLcl substitution/error-policy changes are prohibited")
    if verify:
        if re.search(r"\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|BEGIN|DECLARE|EXEC|EXECUTE|COMMIT|ROLLBACK|GRANT|REVOKE)\b", code, re.IGNORECASE):
            raise BundleError("verification members must be SELECT-only")
```

Keep the comment above `_CLIENT_COMMAND_RE` but update its second sentence, since the reasoning is now about statements rather than lines:

```python
# SQLcl interprets a client command only where a new statement begins. Matching
# these words anywhere in a body rejects CONNECT BY, EXIT WHEN and any column
# named host -- including this template's own control_metadata.sql. Statement
# boundaries come from sql_text.statement_starts, so an inline ";" starts a new
# statement here exactly as it does in SQLcl.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH=scripts python3 -m unittest tests.<module> -v 2>&1 | tail -25`
Expected: PASS, including the four previously-failing cases

- [ ] **Step 7: Run the full suite and lint**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5 && python3 -m ruff check scripts/`
Expected: `OK` then `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add scripts/teamlib/migration_bundle.py scripts/tests/
git commit -m "Check migration members per statement, not per line"
```

---

## Task 4: Make the production read-only guard both stricter and usable

**Files:**
- Modify: `scripts/teamlib/sqlcl.py:88-118` (rewrite `_assert_production_read_only`)
- Modify: `scripts/teamlib/sqlcl.py:17` (import from `sql_text`)
- Test: `scripts/tests/test_sqlcl.py`

Two defects, one function. It lets `SELECT 1 FROM dual; DROP TABLE audit_log;` through, and it refuses `scripts/sql/schema_inventory.sql` because its `in_block` flag is initialised and read but never set — so the `DBMS_METADATA.SET_TRANSFORM_PARAM` setup block at line 4 is treated as a forbidden statement and `check-drift` cannot run against production at all.

The fix keeps the allowlist model. It blanks exactly one narrowly-specified read-only construct — an anonymous block whose every line is a `DBMS_METADATA.SET_TRANSFORM_PARAM` call or the fixed `EXCEPTION/WHEN OTHERS THEN/RAISE` tail — and then refuses everything the allowlist does not name, now per statement.

**Interfaces:**
- Consumes: `sql_text.BLOCK_START_RE`, `sql_text.statement_starts`, `sql_text.mask_sql` (Tasks 1–2).
- Produces: `sqlcl._assert_production_read_only(driver_text: str) -> None` — unchanged signature, raises `SqlclError`.

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_sqlcl.py`:

```python
class ProductionReadOnlyGuardTests(unittest.TestCase):
    def test_second_statement_on_one_line_is_refused(self):
        from teamlib.sqlcl import SqlclError, _assert_production_read_only

        with self.assertRaises(SqlclError) as raised:
            _assert_production_read_only("SELECT 1 FROM dual; DROP TABLE audit_log;\n")
        self.assertIn("not a query or a display setting", str(raised.exception))

    def test_statement_after_an_unterminated_setting_is_refused(self):
        from teamlib.sqlcl import SqlclError, _assert_production_read_only

        with self.assertRaises(SqlclError):
            _assert_production_read_only("SET HEADING OFF\nDROP TABLE audit_log;\n")

    def test_shipped_schema_inventory_is_accepted(self):
        from pathlib import Path

        from teamlib.sqlcl import _assert_production_read_only

        source = Path(__file__).resolve().parents[1] / "sql" / "schema_inventory.sql"
        _assert_production_read_only(source.read_text(encoding="utf-8"))

    def test_a_block_that_is_not_pure_metadata_setup_is_refused(self):
        from teamlib.sqlcl import SqlclError, _assert_production_read_only

        driver = (
            "BEGIN\n"
            "  DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'STORAGE', FALSE);\n"
            "  DELETE FROM audit_log;\n"
            "END;\n"
            "/\n"
        )
        with self.assertRaises(SqlclError):
            _assert_production_read_only(driver)

    def test_plain_queries_and_settings_still_pass(self):
        from teamlib.sqlcl import _assert_production_read_only

        _assert_production_read_only(
            "SET DEFINE OFF\n"
            "SET HEADING OFF\n"
            "WHENEVER SQLERROR EXIT SQL.SQLCODE\n"
            "WITH x AS (SELECT 1 a FROM dual)\n"
            "SELECT a FROM x;\n"
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_sqlcl -k ProductionReadOnlyGuard -v 2>&1 | tail -25`
Expected: FAIL — `test_second_statement_on_one_line_is_refused` and `test_statement_after_an_unterminated_setting_is_refused` raise nothing; `test_shipped_schema_inventory_is_accepted` fails with "not a query or a display setting (line 4)"

- [ ] **Step 3: Rewrite the guard**

In `scripts/teamlib/sqlcl.py`, change the import on line 17 from:

```python
from .sql_text import mask_sql
```

to:

```python
from .sql_text import BLOCK_START_RE, mask_sql, statement_starts
```

Then replace the whole `_assert_production_read_only` function with:

```python
# DBMS_METADATA session transforms change how a definition is rendered for this
# session and touch no data. schema_inventory.sql needs them, and a production
# read of the schema is exactly the operation this guard exists to permit. The
# allowance is deliberately line-oriented and exhaustive: one call per line, and
# any other line inside the block leaves the whole block in place to be refused.
_PRODUCTION_READ_SETUP_LINE_RE = re.compile(
    r"^(?:BEGIN|END;?|EXCEPTION|WHEN\s+OTHERS\s+THEN|RAISE;|"
    r"DBMS_METADATA\.SET_TRANSFORM_PARAM\s*\(.*\);)$",
    re.IGNORECASE,
)


def _blank_metadata_setup_blocks(masked: str) -> str:
    """Blank PL/SQL blocks that only configure DBMS_METADATA session transforms.

    Offsets and line count are preserved so the refusal messages produced by the
    statement walk still name the right line. A block with even one line the
    allowance does not name is left untouched, so the walk sees its ``BEGIN``
    and refuses it.
    """
    lines = masked.splitlines(keepends=True)
    output = list(lines)
    index = 0
    total = len(lines)
    while index < total:
        if not BLOCK_START_RE.match(lines[index].strip()):
            index += 1
            continue
        end = index
        while end < total and lines[end].strip() != "/":
            end += 1
        body = [line.strip() for line in lines[index:end] if line.strip()]
        if body and all(_PRODUCTION_READ_SETUP_LINE_RE.match(item) for item in body):
            for position in range(index, min(end + 1, total)):
                output[position] = "\n" if lines[position].endswith("\n") else ""
        index = end + 1
    return "".join(output)


def _assert_production_read_only(driver_text: str) -> None:
    masked, terminated = mask_sql(driver_text)
    if not terminated:
        raise SqlclError(
            "production read-only SQLcl operation has an unterminated comment or literal"
        )
    masked = _blank_metadata_setup_blocks(masked)
    for number, statement in statement_starts(masked):
        if _PRODUCTION_READ_ALLOWED_RE.match(statement):
            continue
        if _PRODUCTION_READ_SETTINGS_RE.match(statement):
            continue
        if _PRODUCTION_READ_DIRECTIVE_RE.match(statement):
            continue
        raise SqlclError(
            "production read-only SQLcl operation contains a statement that is not a "
            f"query or a display setting (line {number})"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_sqlcl -v 2>&1 | tail -25`
Expected: PASS, 5 new tests plus every pre-existing test in the module

- [ ] **Step 5: Confirm the reported bypass is closed and production drift is unblocked**

Run:

```bash
cd /home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM && PYTHONPATH=scripts python3 -c "
from pathlib import Path
from teamlib.sqlcl import SqlclError, _assert_production_read_only
for label, sql in [
    ('inline DROP', 'SELECT 1 FROM dual; DROP TABLE audit_log;'),
    ('after SET',   'SET HEADING OFF\nDROP TABLE audit_log;'),
    ('inventory',   Path('scripts/sql/schema_inventory.sql').read_text()),
]:
    try:
        _assert_production_read_only(sql); print(f'{label}: ALLOWED')
    except SqlclError as exc:
        print(f'{label}: refused -> {exc}')
"
```

Expected: `inline DROP: refused`, `after SET: refused`, `inventory: ALLOWED`

- [ ] **Step 6: Run the full suite and lint**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5 && python3 -m ruff check scripts/`
Expected: `OK` then `All checks passed!`

- [ ] **Step 7: Document the production read boundary**

Append to `docs/schema-coverage.md`:

```markdown
## What a production read may contain

A production target is read-only, and the read-only guard is an allowlist, not a
denylist. A driver sent to a production target may contain only:

- `SELECT` and `WITH` queries,
- the display settings the generated driver needs (`SET HEADING`, `FEEDBACK`,
  `LINESIZE`, `PAGESIZE`, `LONG`, `ECHO`, `VERIFY`, `DEFINE`, `ENCODING`,
  `TERMOUT`, `TRIMSPOOL`, `SQLBLANKLINES`, `MARKUP`),
- `WHENEVER SQLERROR`/`WHENEVER OSERROR` and `EXIT`,
- one anonymous PL/SQL block whose every line is a
  `DBMS_METADATA.SET_TRANSFORM_PARAM` call or the fixed
  `EXCEPTION`/`WHEN OTHERS THEN`/`RAISE;` tail. This is what
  `scripts/sql/schema_inventory.sql` needs, and it changes session rendering
  only.

Statement boundaries follow SQLcl: an inline `;` starts a new statement, and a
client command ends at the end of its line. Anything the list does not name is
refused before SQLcl is launched.
```

- [ ] **Step 8: Commit**

```bash
git add scripts/teamlib/sqlcl.py scripts/tests/test_sqlcl.py docs/schema-coverage.md
git commit -m "Guard production reads per statement and admit the metadata setup block"
```

---

# Phase 2 — Command paths that always fail

---

## Task 5: Bootstrap release metadata with the real schema-set digest

**Files:**
- Modify: `scripts/teamlib/release_adapter.py:114`
- Test: `scripts/tests/test_release_adapter.py`

`apply_verified_release` computes the schema-set SHA-256 on line 111 and then calls `migration_store.bootstrap(metadata, schema_set_digest="release")`. `SqlMigrationStore.bootstrap` MERGEs that literal into `TEAM_MIGRATION_META.schema_set_digest` with `WHEN MATCHED THEN UPDATE`, so it both breaks this run and overwrites a correct digest written earlier by `migrate --bootstrap`. Every subsequent `record_inventory` compares the stored `'release'` against the manifest's real digest and raises `ORA-20011 SCHEMA_SET_DIGEST_MISMATCH`.

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_release_adapter.py`:

```python
class SchemaSetDigestTests(unittest.TestCase):
    def test_bootstrap_receives_the_computed_schema_set_digest(self):
        import inspect

        from teamlib import release_adapter

        source = inspect.getsource(release_adapter.apply_verified_release)
        self.assertNotIn(
            'schema_set_digest="release"',
            source,
            "apply-release must bootstrap with the computed digest, not a literal; "
            "a literal makes every record_inventory raise ORA-20011",
        )
        self.assertIn("bootstrap(metadata, schema_set_digest=schema_set_digest)", source)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_release_adapter -k SchemaSetDigest -v 2>&1 | tail -15`
Expected: FAIL with `apply-release must bootstrap with the computed digest, not a literal`

- [ ] **Step 3: Pass the computed digest**

In `scripts/teamlib/release_adapter.py`, change line 114 from:

```python
    migration_store.bootstrap(metadata, schema_set_digest="release")
```

to:

```python
    # The digest must be the one the live inventories carry. A placeholder here
    # is written into TEAM_MIGRATION_META and makes every record_inventory in
    # this run -- and every later `migrate` against the same metadata schema --
    # fail with ORA-20011 SCHEMA_SET_DIGEST_MISMATCH.
    migration_store.bootstrap(metadata, schema_set_digest=schema_set_digest)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_release_adapter -v 2>&1 | tail -15`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add scripts/teamlib/release_adapter.py scripts/tests/test_release_adapter.py
git commit -m "Bootstrap release metadata with the real schema-set digest"
```

---

## Task 6: Give `deploy-app` the APEX timeout budget

**Files:**
- Modify: `scripts/teamlib/deploy.py:16` (import), `:43`, `:113`
- Test: `scripts/tests/test_deploy.py`

`apex.py` passes `timeout=APEX_TIMEOUT_SECONDS` (1800 s) to both its export and its import "because a full APEX application export or import is not comparable to a metadata query" and "timing one out marks the shared application uncertain, which stops the whole team". `deploy.py` performs the same two operations and passes no timeout, so it runs on `DEFAULT_TIMEOUT_SECONDS` (120 s). A timeout after `mark_payload_starting` leaves the destination mutex held and `is_uncertain = 1`.

**Interfaces:**
- Consumes: `sqlcl.APEX_TIMEOUT_SECONDS` (existing).
- Produces: `deploy._capture_destination` and `deploy.deploy_app` — no signature change.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_deploy.py`:

```python
class DeployTimeoutTests(unittest.TestCase):
    def test_every_apex_operation_gets_the_application_budget(self):
        import inspect

        from teamlib import deploy
        from teamlib.sqlcl import APEX_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS

        self.assertGreater(APEX_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)
        source = inspect.getsource(deploy)
        runner_calls = [
            line.strip()
            for line in source.splitlines()
            if "runner(target," in line
        ]
        self.assertTrue(runner_calls, "deploy.py must still drive SQLcl through runner()")
        for call in runner_calls:
            self.assertIn(
                "timeout=APEX_TIMEOUT_SECONDS",
                call,
                f"APEX operation runs on the metadata budget: {call}",
            )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_deploy -k DeployTimeout -v 2>&1 | tail -15`
Expected: FAIL with `APEX operation runs on the metadata budget: result = runner(target, "read", driver, work)`

- [ ] **Step 3: Import the budget and apply it**

In `scripts/teamlib/deploy.py`, change the import on line 16 from:

```python
from .sqlcl import run_sqlcl, SqlclError
```

to:

```python
from .sqlcl import APEX_TIMEOUT_SECONDS, run_sqlcl, SqlclError
```

Change line 43 from:

```python
    result = runner(target, "read", driver, work)
```

to:

```python
    result = runner(target, "read", driver, work, timeout=APEX_TIMEOUT_SECONDS)
```

Change line 113 from:

```python
        result = runner(target, "write", driver, work)
```

to:

```python
        result = runner(target, "write", driver, work, timeout=APEX_TIMEOUT_SECONDS)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_deploy -v 2>&1 | tail -15`
Expected: PASS

- [ ] **Step 5: Confirm no test double breaks on the new keyword**

Run: `grep -rn "def runner\|def fake_runner\|runner=" scripts/tests/test_deploy.py | head -20`

Every stub in this module must accept `**kwargs` or an explicit `timeout=None`. If any stub is declared as `def runner(target, operation, driver, work):`, add `, *, timeout=None` to it. Re-run Step 4 after any edit.

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add scripts/teamlib/deploy.py scripts/tests/test_deploy.py
git commit -m "Deploy APEX applications on the application timeout budget"
```

---

## Task 7: Apply the tree exclusion list to the cleanliness check

**Files:**
- Modify: `scripts/teamlib/trees.py:248-249`
- Test: `scripts/tests/test_trees.py`

`_read_regular_tree` and `read_git_tree` both call `_is_excluded`, which skips `deployments/`, `logs/`, `.logs/` and the three export-log filenames — those paths are not owned source. `assert_source_clean` runs `git status --ignored=matching` and skips only `deployments/`, so a `.gitignore`-managed `apps/<alias>/export.log` reports `!!` and raises `owned source is not clean`. That blocks `export-app`, `import-app`, `adopt-app` and `bootstrap-app` for a developer whose APEX export dropped its own log next to the application.

The `--ignored=matching` flag stays: it exists to catch an ignored file shadowing a genuinely owned path, and everything outside `_is_excluded` is still refused.

**Interfaces:**
- Consumes: `trees._is_excluded` (existing, unchanged).
- Produces: `trees.assert_source_clean(repo, alias) -> None` — unchanged signature.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_trees.py`:

```python
class SourceCleanExclusionTests(unittest.TestCase):
    def _repo(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="team-clean-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        subprocess.run(["git", "init", "-q", str(directory)], check=True)
        subprocess.run(["git", "-C", str(directory), "config", "user.email", "x@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(directory), "config", "user.name", "x"], check=True)
        app = directory / "apps" / "demo" / ".apex"
        app.mkdir(parents=True)
        (directory / "apps" / "demo" / "application.apx").write_bytes(b"x")
        (app / "apexlang.json").write_bytes(b"{}")
        (directory / ".gitignore").write_text("apps/demo/export.log\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "-C", str(directory), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(directory), "commit", "-qm", "seed"], check=True)
        return directory

    def test_ignored_export_log_does_not_make_source_dirty(self):
        repo = self._repo()
        (repo / "apps" / "demo" / "export.log").write_text("APEX export log\n", encoding="utf-8", newline="\n")
        assert_source_clean(repo, "demo")

    def test_ignored_file_that_is_not_excluded_still_fails(self):
        repo = self._repo()
        (repo / ".gitignore").write_text(
            "apps/demo/export.log\napps/demo/shadow.sql\n", encoding="utf-8", newline="\n"
        )
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "ignore"], check=True)
        (repo / "apps" / "demo" / "shadow.sql").write_text("select 1 from dual;\n", encoding="utf-8", newline="\n")
        with self.assertRaises(TreeError):
            assert_source_clean(repo, "demo")
```

Confirm the module's imports include `shutil`, `subprocess`, `tempfile`, `Path`, `assert_source_clean` and `TreeError`; add any that are missing to the existing import block at the top of the file.

- [ ] **Step 2: Run the tests to verify one fails**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_trees -k SourceCleanExclusion -v 2>&1 | tail -20`
Expected: `test_ignored_export_log_does_not_make_source_dirty` FAILS with `TreeError: owned source is not clean: apps/demo/export.log`; `test_ignored_file_that_is_not_excluded_still_fails` PASSES

- [ ] **Step 3: Reuse the exclusion predicate**

In `scripts/teamlib/trees.py`, replace lines 248–249:

```python
        relative = _validate_relative_path(path[len(prefix):])
        if relative.startswith("deployments/") or relative == "deployments":
            continue
```

with:

```python
        relative = _validate_relative_path(path[len(prefix):])
        # The same paths the tree readers skip are not owned source, so a
        # status entry for one of them is not an uncleanliness. Anything the
        # predicate does not name -- including an ignored file shadowing a real
        # owned path -- is still refused below.
        if _is_excluded(relative) or relative == "deployments":
            continue
```

- [ ] **Step 4: Run the tests to verify both pass**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_trees -v 2>&1 | tail -20`
Expected: PASS, both new tests plus every pre-existing test in the module

- [ ] **Step 5: Run the full suite and lint**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5 && python3 -m ruff check scripts/`
Expected: `OK` then `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add scripts/teamlib/trees.py scripts/tests/test_trees.py
git commit -m "Stop treating an excluded export log as unclean owned source"
```

---

## Task 8: Install `cryptography` where the runbook is generated

**Files:**
- Modify: `.github/workflows/release.yml` (the `test-and-handoff` job)
- Test: `scripts/tests/test_docs.py`

`gen-runbook` verifies the detached Ed25519 signature over the test evidence and raises `RunbookDependencyError` without `cryptography`. The package is declared in the `promotion` extra but no workflow installs it, so the production handoff step fails on every tagged release.

**Interfaces:**
- Consumes: the `promotion` extra declared in `pyproject.toml`.
- Produces: no Python API change.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_docs.py`:

```python
class ReleaseWorkflowDependencyTests(unittest.TestCase):
    def test_runbook_job_installs_the_promotion_extra(self):
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("gen-runbook", text)
        self.assertIn(
            "pip install --quiet 'cryptography>=41'",
            text,
            "gen-runbook verifies an Ed25519 signature and fails without cryptography",
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_docs -k ReleaseWorkflowDependency -v 2>&1 | tail -15`
Expected: FAIL with `gen-runbook verifies an Ed25519 signature and fails without cryptography`

- [ ] **Step 3: Add the install step**

In `.github/workflows/release.yml`, in the `test-and-handoff` job, insert this step immediately after the `actions/setup-python@v5` step and before the `actions/download-artifact@v4` step:

```yaml
      - name: Install the signed-handoff verification dependency
        run: |
          # gen-runbook verifies a detached Ed25519 signature over the test
          # evidence. The daily workflow is stdlib-only; this is the one
          # command that needs the promotion extra.
          python3 -m pip install --quiet 'cryptography>=41'
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_docs -v 2>&1 | tail -15`
Expected: PASS

- [ ] **Step 5: Verify the workflow is still valid YAML**

Run:

```bash
cd /home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM && python3 -c "
import json, re, sys
from pathlib import Path
text = Path('.github/workflows/release.yml').read_text(encoding='utf-8')
assert '\r' not in text, 'CR found; template-checks forbids it'
indents = [len(line) - len(line.lstrip()) for line in text.splitlines() if line.strip().startswith('- name:')]
print('step indents:', sorted(set(indents)))
"
```

Expected: `step indents: [6]` — every step in the file, including the new one, is indented six spaces

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/release.yml scripts/tests/test_docs.py
git commit -m "Install the promotion extra before generating the runbook"
```

---

# Phase 3 — Make the shipped CI reachable

`ci/app-checks/README.md` states the intended contract: the replay runner "treats missing declarations, missing fixtures and missing SQL/browser adapters as qualification failures. It never converts an unavailable check into a pass." That is correct and stays. What is missing is the SQL adapter the repository is capable of supplying, and a refusal message that names what an adopting team must provide. The template's own CI is unaffected today because `apps/` holds only `.gitkeep`, so `aliases` is empty and the zero-app coverage stub is returned.

---

## Task 9: Supply a real SELECT check runner to the replay adapter

**Files:**
- Modify: `scripts/ci_replay_runner.py` (add `_select_runner`, wire into `replay_target`)
- Test: `scripts/tests/test_app_checks.py`

**Interfaces:**
- Consumes: `app_checks.verify_candidate_apps`, `sqlcl.run_sqlcl`, `config.profile_target`.
- Produces: `ci_replay_runner._select_runner(config, repo, work) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]` — the returned callable takes `(alias, check)` and returns `{"status": "PASS"|"FAIL", "diagnostic": str, "assertions": list[str]}`.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_app_checks.py`:

```python
class SelectRunnerTests(unittest.TestCase):
    def test_all_pass_rows_produce_a_pass(self):
        import ci_replay_runner

        recorded = {}

        def fake_run_sqlcl(target, operation, driver, work, **kwargs):
            recorded["operation"] = operation
            recorded["driver"] = Path(driver).read_text(encoding="utf-8")

            class Result:
                stdout = "TEAM_ASSERT|employee_table_exists|PASS\nTEAM_ASSERT|dept_fk|PASS\n"

            return Result()

        root = Path(tempfile.mkdtemp(prefix="team-select-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        checks = root / "ci" / "app-checks" / "employee"
        checks.mkdir(parents=True)
        (checks.parent / "employee" / "employee-table.verify.sql").write_text(
            "SELECT 'employee_table_exists' assertion_name, 'PASS' status FROM dual;\n",
            encoding="utf-8",
            newline="\n",
        )
        runner = ci_replay_runner._select_runner(
            profile=object(), repo=root, work=root / "work", run_sqlcl=fake_run_sqlcl
        )
        observed = runner("employee", {"id": "t1", "verify_sql": "employee/employee-table.verify.sql"})
        self.assertEqual(observed["status"], "PASS")
        self.assertEqual(recorded["operation"], "read")

    def test_a_fail_row_produces_a_fail_with_the_assertion_named(self):
        import ci_replay_runner

        def fake_run_sqlcl(target, operation, driver, work, **kwargs):
            class Result:
                stdout = "TEAM_ASSERT|dept_fk|FAIL\n"

            return Result()

        root = Path(tempfile.mkdtemp(prefix="team-select-fail-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        checks = root / "ci" / "app-checks" / "employee"
        checks.mkdir(parents=True)
        (checks / "dept.verify.sql").write_text(
            "SELECT 'dept_fk' assertion_name, 'FAIL' status FROM dual;\n",
            encoding="utf-8",
            newline="\n",
        )
        runner = ci_replay_runner._select_runner(
            profile=object(), repo=root, work=root / "work", run_sqlcl=fake_run_sqlcl
        )
        observed = runner("employee", {"id": "t2", "verify_sql": "employee/dept.verify.sql"})
        self.assertEqual(observed["status"], "FAIL")
        self.assertIn("dept_fk", observed["diagnostic"])

    def test_a_missing_verify_member_is_a_fail_not_an_unknown(self):
        import ci_replay_runner

        root = Path(tempfile.mkdtemp(prefix="team-select-missing-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "ci" / "app-checks").mkdir(parents=True)
        runner = ci_replay_runner._select_runner(
            profile=object(), repo=root, work=root / "work", run_sqlcl=lambda *a, **k: None
        )
        observed = runner("employee", {"id": "t3", "verify_sql": "employee/absent.verify.sql"})
        self.assertEqual(observed["status"], "FAIL")
        self.assertIn("absent.verify.sql", observed["diagnostic"])
```

Confirm the module's import block has `shutil`, `tempfile` and `Path`; add any that are missing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_app_checks -k SelectRunner -v 2>&1 | tail -20`
Expected: FAIL with `AttributeError: module 'ci_replay_runner' has no attribute '_select_runner'`

- [ ] **Step 3: Implement the runner**

In `scripts/ci_replay_runner.py`, add after `_check_declarations`:

```python
_ASSERTION_RE = re.compile(r"^TEAM_ASSERT\|([^|]+)\|(PASS|FAIL)$")


def _select_runner(*, profile, repo: Path, work: Path, run_sqlcl=run_sqlcl):
    """Return a qualified SELECT-check adapter for verify_candidate_apps.

    A declared ``select`` check names a ``.verify.sql`` member beside its
    declaration. The member is already constrained to SELECT-only assertions
    that project ``assertion_name`` and ``status``; this runner executes it
    through the read-only VERIFY profile and reports PASS only when every
    returned row says PASS. A member that cannot be read is a FAIL, never an
    UNKNOWN -- an unavailable check must never look like a passing one.
    """
    checks_root = Path(repo) / "ci" / "app-checks"

    def resolve(alias: str, check: Mapping[str, Any]) -> dict[str, Any]:
        relative = str(check.get("verify_sql", ""))
        member = checks_root / relative
        if member.is_symlink() or not member.is_file():
            return {
                "status": "FAIL",
                "diagnostic": f"verification member is missing: {relative}",
            }
        driver_root = Path(work) / "app-checks" / alias / str(check.get("id", "check"))
        driver_root.mkdir(parents=True, exist_ok=True)
        driver = driver_root / "verify.sql"
        driver.write_text(
            "SET DEFINE OFF\nSET HEADING OFF\nSET FEEDBACK OFF\nSET PAGESIZE 0\n"
            + member.read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )
        try:
            result = run_sqlcl(profile, "read", driver, driver_root)
        except Exception as exc:  # noqa: BLE001 - any adapter failure is a check failure
            return {"status": "FAIL", "diagnostic": f"verification query failed: {exc}"}
        rows = []
        for raw in getattr(result, "stdout", "").splitlines():
            match = _ASSERTION_RE.match(raw.strip())
            if match:
                rows.append((match.group(1), match.group(2)))
        if not rows:
            return {
                "status": "FAIL",
                "diagnostic": "verification query returned no TEAM_ASSERT rows",
            }
        failures = [name for name, status in rows if status != "PASS"]
        if failures:
            return {
                "status": "FAIL",
                "diagnostic": "failed assertions: " + ", ".join(sorted(failures)),
                "assertions": [name for name, _ in rows],
            }
        return {
            "status": "PASS",
            "diagnostic": "",
            "assertions": [name for name, _ in rows],
        }

    return resolve
```

Add `import re` and `from collections.abc import Mapping` and `from typing import Any` to the module's import block if they are not already present.

- [ ] **Step 4: Wire it into the replay target**

In `scripts/ci_replay_runner.py`, in `main`, change the `replay_target` construction from:

```python
            replay_target = {
                **target_data,
                "role": config.role,
                "environment": config.environment,
                "instance_id": config.profiles["TABLES"].expected_instance_id,
                "app_ids": config.apps,
            }
```

to:

```python
            replay_target = {
                **target_data,
                "role": config.role,
                "environment": config.environment,
                "instance_id": config.profiles["TABLES"].expected_instance_id,
                "app_ids": config.apps,
                "select_runner": _select_runner(
                    profile=profile_target(config, "VERIFY"),
                    repo=repo,
                    work=work,
                ),
            }
```

- [ ] **Step 5: Document the assertion framing**

Append to `ci/app-checks/README.md`:

```markdown
## What a `select` check must project

The replay runner executes a `select` check's `.verify.sql` member through the
read-only VERIFY profile and reads framed assertion rows:

```sql
SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status
  FROM (SELECT 'employee_table_exists' assertion_name,
               CASE WHEN COUNT(*) = 1 THEN 'PASS' ELSE 'FAIL' END status
          FROM all_tables WHERE table_name = 'EMPLOYEE');
```

The check passes only when every returned row says `PASS`. A member that
returns no framed rows, cannot be read, or whose query fails is a **FAIL**, not
an UNKNOWN.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_app_checks -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 7: Run the full suite and lint**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5 && python3 -m ruff check scripts/`
Expected: `OK` then `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add scripts/ci_replay_runner.py scripts/tests/test_app_checks.py ci/app-checks/README.md
git commit -m "Run declared SELECT checks through the VERIFY profile"
```

---

## Task 10: Name the missing flow adapter instead of reporting UNKNOWN

**Files:**
- Modify: `scripts/ci_replay_runner.py` (add `_flow_runner`, wire it, add the refusal)
- Modify: `.env.example` (document `TEAM_FLOW_RUNNER`)
- Test: `scripts/tests/test_app_checks.py`

A `flow` check needs a browser driver, which a stdlib-only template cannot ship. Today its absence surfaces as a generic per-check `UNKNOWN` that rolls up to `FAIL` with the message "qualified check runner is unavailable" — true, but it does not tell an adopting team what to provide. Make it an explicit, early refusal that names the environment variable and the contract.

**Interfaces:**
- Consumes: Task 9's `_select_runner` wiring.
- Produces: `ci_replay_runner._flow_runner(executable: str, work: Path) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]`. The external executable is invoked as `<executable> --alias <alias> --check-json <path>` and must print one JSON object with a `status` of `PASS`, `FAIL` or `UNKNOWN`.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_app_checks.py`:

```python
class FlowRunnerTests(unittest.TestCase):
    def test_absent_flow_adapter_is_named_in_the_refusal(self):
        import ci_replay_runner

        with self.assertRaises(SystemExit) as raised:
            ci_replay_runner._require_flow_adapter(
                {"employee": {"checks": [{"id": "smoke", "kind": "flow"}]}}, None
            )
        message = str(raised.exception)
        self.assertIn("TEAM_FLOW_RUNNER", message)
        self.assertIn("employee/smoke", message)

    def test_no_flow_checks_needs_no_adapter(self):
        import ci_replay_runner

        ci_replay_runner._require_flow_adapter(
            {"employee": {"checks": [{"id": "t1", "kind": "select"}]}}, None
        )

    def test_flow_runner_parses_the_adapter_result(self):
        import ci_replay_runner

        root = Path(tempfile.mkdtemp(prefix="team-flow-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        adapter = root / "flow.sh"
        adapter.write_text(
            '#!/usr/bin/env bash\necho \'{"status": "PASS", "diagnostic": ""}\'\n',
            encoding="utf-8",
            newline="\n",
        )
        adapter.chmod(0o755)
        runner = ci_replay_runner._flow_runner(str(adapter), root)
        self.assertEqual(runner("employee", {"id": "smoke", "kind": "flow"})["status"], "PASS")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_app_checks -k FlowRunner -v 2>&1 | tail -20`
Expected: FAIL with `AttributeError: module 'ci_replay_runner' has no attribute '_require_flow_adapter'`

- [ ] **Step 3: Implement both helpers**

In `scripts/ci_replay_runner.py`, add after `_select_runner`:

```python
def _require_flow_adapter(declarations: Mapping[str, Any], executable: str | None) -> None:
    """Refuse early, and by name, when a declared flow check has no adapter.

    A browser driver cannot ship with a stdlib-only template. Leaving its
    absence to surface as a per-check UNKNOWN produced a correct refusal with an
    unactionable message, so name the checks and the variable instead.
    """
    if executable:
        return
    pending = [
        f"{alias}/{check.get('id')}"
        for alias, declaration in sorted(declarations.items())
        for check in declaration.get("checks", [])
        if check.get("kind") == "flow"
    ]
    if not pending:
        return
    raise SystemExit(
        "declared flow checks have no qualified browser adapter: "
        + ", ".join(pending)
        + ". Set TEAM_FLOW_RUNNER to an executable invoked as "
        "`<executable> --alias <alias> --check-json <path>` that prints one JSON "
        "object with a status of PASS, FAIL or UNKNOWN."
    )


def _flow_runner(executable: str, work: Path):
    """Return a flow-check adapter that delegates to a qualified executable."""

    def resolve(alias: str, check: Mapping[str, Any]) -> dict[str, Any]:
        payload_root = Path(work) / "flow" / alias
        payload_root.mkdir(parents=True, exist_ok=True)
        payload = payload_root / f"{check.get('id', 'check')}.json"
        payload.write_text(
            json.dumps(dict(check), sort_keys=True), encoding="utf-8", newline="\n"
        )
        try:
            result = subprocess.run(
                [executable, "--alias", alias, "--check-json", str(payload)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return {"status": "FAIL", "diagnostic": f"flow adapter could not start: {exc}"}
        if result.returncode != 0:
            return {
                "status": "FAIL",
                "diagnostic": result.stderr.strip() or f"flow adapter exit {result.returncode}",
            }
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return {"status": "FAIL", "diagnostic": f"flow adapter did not return JSON: {exc}"}
        if not isinstance(value, dict):
            return {"status": "FAIL", "diagnostic": "flow adapter returned a non-object"}
        return value

    return resolve
```

Add `import os` to the module's import block if it is not already present.

- [ ] **Step 4: Wire the adapter and the refusal into `main`**

In `main`, immediately after `declarations = _check_declarations(repo, aliases)`, insert:

```python
            loaded = {
                alias: json.loads(path.read_text(encoding="utf-8"))
                for alias, path in declarations.items()
            }
            flow_executable = os.environ.get("TEAM_FLOW_RUNNER") or None
            _require_flow_adapter(loaded, flow_executable)
```

Then extend the `replay_target` dict from Task 9 with:

```python
                "flow_runner": _flow_runner(flow_executable, work) if flow_executable else None,
```

`app_checks._runner` returns the value only when it is callable, so a `None` here behaves exactly as an absent key — but `_require_flow_adapter` has already refused before that can matter.

- [ ] **Step 5: Document the variable**

Append to `.env.example`:

```
# Optional. Path to a qualified browser adapter used by ci-replay for declared
# `flow` application checks. Invoked as
#   <executable> --alias <alias> --check-json <path>
# and must print one JSON object with a status of PASS, FAIL or UNKNOWN.
# Unset is safe until a project declares its first flow check.
# TEAM_FLOW_RUNNER=/opt/team/flow-adapter
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_app_checks -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 7: Run the full suite and lint**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5 && python3 -m ruff check scripts/`
Expected: `OK` then `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add scripts/ci_replay_runner.py scripts/tests/test_app_checks.py .env.example
git commit -m "Name the missing flow adapter instead of reporting UNKNOWN checks"
```

---

## Task 11: Let a fresh environment adopt its observed frontier

**Files:**
- Modify: `scripts/team.py` (parser entry, `_online` branch)
- Modify: `.github/workflows/integration.yml` (insert the step)
- Test: `scripts/tests/test_integration_workflow.py`

`check-drift` reports `status: unknown` and exits 3 whenever no observed migration frontier exists. The sequence-0 observation is created only by `apply_plan`'s per-migration loop inside `migrate`, which never runs its body when there are no pending migrations. `integration.yml` runs `setup-state` then `check-drift` then `migrate`, so on a freshly provisioned integration database the drift step always fails and the deploy never runs. There is no adoption step anywhere in the workflow to break the cycle.

Add an explicit `adopt-frontier` command rather than weakening `check-drift`: the frontier is *observed* state, and something has to observe it.

**Interfaces:**
- Consumes: `migration_store.SqlMigrationStore` methods `bootstrap`, `acquire`, `record_inventory`, `ensure_observation`, `release`; `live_inventory.inventory_target`.
- Produces: `team.py adopt-frontier [--env FILE]` — exits 0 and prints `{"status": "success", "operation": "adopt-frontier", "inventory_digest": "<sha256>"}`, or exits 3 with a refusal on stderr.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_integration_workflow.py`:

```python
class FrontierAdoptionTests(unittest.TestCase):
    def test_adopt_frontier_is_a_known_command(self):
        import team

        parsed = team._parser().parse_args(["adopt-frontier"])
        self.assertEqual(parsed.command, "adopt-frontier")

    def test_adopt_frontier_is_a_production_refused_command(self):
        import team

        self.assertIn("adopt-frontier", team.PRODUCTION_REFUSED_COMMANDS)
        self.assertIn("setup-state", team.PRODUCTION_REFUSED_COMMANDS)

    def test_integration_workflow_adopts_before_it_checks_drift(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "integration.yml").read_text(encoding="utf-8")
        self.assertIn("adopt-frontier", text)
        self.assertLess(
            text.index("adopt-frontier"),
            text.index("check-drift"),
            "check-drift exits 3 until a frontier exists, so adoption must come first",
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_integration_workflow -k FrontierAdoption -v 2>&1 | tail -20`
Expected: FAIL — `adopt-frontier` is not in the parser choices

- [ ] **Step 3: Register the command**

In `scripts/team.py`, add near the other module-level constants (immediately after the `from teamlib.trees import ...` import block):

```python
# Commands that write controller or metadata state and are therefore refused for
# a production classification. Kept in one place so the parser, the online
# dispatcher and the tests cannot drift apart.
PRODUCTION_REFUSED_COMMANDS = frozenset(
    {"setup-state", "adopt-frontier", "recover-migration", "register-app", "recover-app-lock"}
)
```

In `_parser`, add beside `sub.add_parser("setup-state", parents=[env_parent])`:

```python
    sub.add_parser("adopt-frontier", parents=[env_parent])
```

- [ ] **Step 4: Implement the online branch**

In `_online`, insert immediately after the `setup-state` branch's `return 0`:

```python
    if command == "adopt-frontier":
        config = _config(args, require_verify=True)
        if config.environment == "production" and command in PRODUCTION_REFUSED_COMMANDS:
            raise ConfigError(f"{command} is refused for production targets")
        metadata = profile_target(config, "METADATA")
        store = _sql_migration_store(repo, metadata)
        schema_set_digest = hashlib.sha256(
            f"{config.tables_schema}|{config.code_schema}|{config.metadata_schema}".encode("ascii")
        ).hexdigest()
        store.bootstrap(metadata, schema_set_digest=schema_set_digest)
        inventory = inventory_target(
            profile_target(config, "TABLES"),
            config.tables_schema,
            config.code_schema,
            repo / "scratch" / "frontier-inventory",
            schema_set_digest=schema_set_digest,
        )
        run_token = uuid.uuid4().hex
        store.acquire(metadata, run_token, os.environ.get("USER", "frontier-worker"), socket.gethostname())
        try:
            digest = store.record_inventory(metadata, inventory.as_dict(), run_token=run_token)
            store.ensure_observation(metadata, digest, run_token=run_token)
        finally:
            # A failure here leaves the mutex held on purpose; recover-migration
            # clears it with evidence, exactly as a failed migrate does.
            store.release(metadata, run_token)
        _json({"status": "success", "operation": command, "inventory_digest": digest})
        return 0
```

Change the existing `setup-state` production guard to use the same shared constant, replacing:

```python
        if config.environment == "production":
            raise ConfigError("setup-state is refused for production targets")
```

with:

```python
        if config.environment == "production" and command in PRODUCTION_REFUSED_COMMANDS:
            raise ConfigError(f"{command} is refused for production targets")
```

Do the same for the two remaining sites so the constant is the single source of truth. Replace:

```python
        if command == "recover-migration" and config.environment == "production":
            raise ConfigError("recover-migration is refused for production targets")
```

with:

```python
        if config.environment == "production" and command in PRODUCTION_REFUSED_COMMANDS:
            raise ConfigError(f"{command} is refused for production targets")
```

and replace:

```python
        if target.environment == "production" and command in {"register-app", "recover-app-lock"}:
            raise ConfigError(f"{command} is refused for production targets")
```

with:

```python
        if target.environment == "production" and command in PRODUCTION_REFUSED_COMMANDS:
            raise ConfigError(f"{command} is refused for production targets")
```

`PRODUCTION_REFUSED_COMMANDS` already names exactly `register-app` and `recover-app-lock` among the commands reachable from that branch, so this preserves the current behaviour — `capture-app`, `bootstrap-app`, `adopt-app`, `export-app`, `import-app` and `app-status` keep their own guards inside `apex.py` and `deploy.py`.

- [ ] **Step 5: Run the tests to verify the first two pass**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_integration_workflow -k FrontierAdoption -v 2>&1 | tail -20`
Expected: the first two tests PASS; `test_integration_workflow_adopts_before_it_checks_drift` still FAILS

- [ ] **Step 6: Insert the workflow step**

In `.github/workflows/integration.yml`, in the `deploy` job, insert this step between "Initialize the isolated integration metadata state" and "Check observed drift for this exact SHA":

```yaml
      - name: Adopt the observed migration frontier if none exists
        env:
          TEAM_ENV_FILE: ${{ vars.TEAM_ENV_FILE }}
        run: |
          # check-drift compares live structure against the accepted observed
          # frontier and exits 3 when none has been adopted. A freshly
          # provisioned integration database has no frontier and no pending
          # migrations to create one, so it is observed here explicitly.
          PYTHONPATH=scripts python3 scripts/team.py --env "$TEAM_ENV_FILE" adopt-frontier
```

- [ ] **Step 7: Run the tests to verify all three pass**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_integration_workflow -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 8: Document the command**

Append to `docs/ci.md`:

```markdown
## First run against a new environment

`check-drift` compares live structure against the accepted observed frontier and
exits 3 while no frontier has been adopted. A new database has none, and a
migration run creates one only as a side effect of applying a migration — so a
project with nothing pending can never reach a clean drift check on its own.

Run `team.py adopt-frontier` once, after `setup-state`. It bootstraps the
migration metadata, takes one read-only inventory through the TABLES profile,
records it as an immutable manifest and writes the sequence-0 observation. It is
refused for a production classification, and it does not write any schema object.
```

- [ ] **Step 9: Run the full suite and lint**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5 && python3 -m ruff check scripts/`
Expected: `OK` then `All checks passed!`

- [ ] **Step 10: Commit**

```bash
git add scripts/team.py .github/workflows/integration.yml scripts/tests/test_integration_workflow.py docs/ci.md
git commit -m "Adopt an observed frontier so a new environment can pass check-drift"
```

---

# Phase 4 — Error contracts

---

## Task 12: Return the sync state from the online mutex acquisition

**Files:**
- Modify: `scripts/teamlib/control_store.py:572-606`
- Test: `scripts/tests/test_control_store.py`

`SqlControlStore.acquire_app` is annotated `-> SyncState` and mirrors `ControlStore.acquire_app`, but has no `return` after `self._run("write", payload)`, so it returns `None`. Its three sibling transitions all end with `return self.read_app_sync_state(target_key)`. Today's callers discard the value, so the divergence is latent — a caller written against the file-backed contract gets `AttributeError: 'NoneType' object has no attribute 'is_uncertain'` only against the online store.

**Interfaces:**
- Consumes: `SqlControlStore.read_app_sync_state` (existing).
- Produces: `SqlControlStore.acquire_app(...) -> SyncState` — now honours its annotation.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_control_store.py`:

```python
class SqlControlStoreContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-sql-control-")
        self.addCleanup(self.temp.cleanup)
        self.metadata = Target(
            project="team-template", role="developer", environment="development",
            connection="docker-demo", instance_id="FREE", db_name="FREEPDB1",
            service="freep1", session_user="META", current_schema="META",
            alias=None, workspace_id=None, app_id=None, parsing_schema=None,
            ownership_mode="shared", binding_digest="a" * 64,
        )

    @staticmethod
    def _mutex_stdout(target_key: str) -> str:
        # read_app_sync_state parses eight base64 fields; NULL is encoded as
        # CHR(1) by b64_sql and decoded back to "".
        fields = [target_key, "", "", "", "", "", "1", "0"]
        encoded = "|".join(
            base64.b64encode((value or "\x01").encode("utf-8")).decode("ascii")
            for value in fields
        )
        return f"TEAM_MUTEX|{encoded}\n"

    def _store(self) -> SqlControlStore:
        stdout = self._mutex_stdout("key")

        def fake_runner(target, operation, driver, work, **kwargs):
            class Result:
                pass

            result = Result()
            result.stdout = stdout
            return result

        return SqlControlStore(self.metadata, runner=fake_runner, work_root=Path(self.temp.name))

    def test_acquire_app_returns_a_sync_state(self):
        observed = self._store().acquire_app("key", "run", "checkout", "host", "user")
        self.assertIsInstance(
            observed,
            SyncState,
            "acquire_app is annotated -> SyncState; returning None diverges from "
            "ControlStore.acquire_app and breaks any caller that reads the result",
        )
        self.assertEqual(observed.target_key, "key")

    def test_every_mutex_transition_returns_a_state(self):
        import inspect

        for name in ("acquire_app", "mark_payload_starting", "release_app", "recover_app_lock"):
            source = inspect.getsource(getattr(SqlControlStore, name))
            self.assertIn(
                "return self.read_app_sync_state(target_key)",
                source,
                f"{name} is annotated -> SyncState but does not return one",
            )
```

Add `import base64` to the module's import block, and add `SyncState` to the existing `from teamlib.control_store import (...)` list. `Target`, `Path`, `tempfile` and `unittest` are already imported there.

This fixture is verified: against the current code `acquire_app` returns `None` while `read_app_sync_state` on the same stub returns `SyncState(target_key='key', ..., generation=1, is_uncertain=False)`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_control_store -k SqlControlStoreContract -v 2>&1 | tail -20`
Expected: FAIL with `acquire_app is annotated -> SyncState but does not return one`

- [ ] **Step 3: Return the state**

In `scripts/teamlib/control_store.py`, at the end of `SqlControlStore.acquire_app`, after the `except ControlStoreError` block's final bare `raise`, add at the method's outer indentation:

```python
        return self.read_app_sync_state(target_key)
```

The method's tail must read:

```python
                raise MutexHeld(
                    f"app target is held by {owner or 'another worker'}; recovery owner: {recovery_role or 'configured recovery owners'}",
                    owner_token=owner, recovery_role=recovery_role,
                ) from exc
            raise
        return self.read_app_sync_state(target_key)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_control_store -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add scripts/teamlib/control_store.py scripts/tests/test_control_store.py
git commit -m "Return the sync state from the online mutex acquisition"
```

---

## Task 13: Type-check the target binding connection

**Files:**
- Modify: `scripts/teamlib/config.py:593-596`
- Test: `scripts/tests/test_config.py`

`parse_target_contract` passes `binding["connection"]` straight to `_validate_connection`, so a non-string value reaches `re.fullmatch` and raises `TypeError: expected string or bytes-like object, got 'int'`. `TypeError` is not in `team.py`'s caught tuple, so a malformed contract produces a traceback instead of the structured refusal every other field in this parser produces.

**Interfaces:**
- Consumes: `config._validate_connection` (existing).
- Produces: `config.parse_target_contract` — same signature, now raises `ConfigError` for this case.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_config.py`:

```python
class TargetBindingTypeTests(unittest.TestCase):
    def _contract(self, connection):
        return {
            "version": 1,
            "project": "team",
            "role": "integration",
            "environment": "test",
            "instance_id": "INST",
            "db_name": "DB",
            "service": "svc",
            "session_user": "APP",
            "current_schema": "APP",
            "workspace_id": 1,
            "app_ids": {"checkout": 101},
            "recovery_owner": {"role": "owners", "members": ["a", "b"]},
            "binding": {"connection": connection},
        }

    def test_non_string_connection_is_a_config_error(self):
        for value in (5, True, ["alias"], {"name": "alias"}):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    parse_target_contract(self._contract(value))

    def test_valid_connection_still_parses(self):
        contract = parse_target_contract(self._contract("integration-alias"))
        self.assertEqual(contract.binding["connection"], "integration-alias")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_config -k TargetBindingType -v 2>&1 | tail -20`
Expected: FAIL with `TypeError: expected string or bytes-like object, got 'int'` (an error, not a clean assertion failure)

- [ ] **Step 3: Check the type before the regex**

In `scripts/teamlib/config.py`, replace:

```python
    connection = binding.get("connection", binding.get("sqlcl_connection"))
    if connection is not None:
        _validate_connection("binding.connection", connection)
```

with:

```python
    connection = binding.get("connection", binding.get("sqlcl_connection"))
    if connection is not None:
        # Every other field in this parser is type-checked before it reaches a
        # regex. Without this, a JSON number here escapes the ConfigError
        # contract as a TypeError and team.py prints a traceback.
        if not isinstance(connection, str):
            raise ConfigError("target binding.connection must be a credential-free connection name")
        _validate_connection("binding.connection", connection)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_config -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add scripts/teamlib/config.py scripts/tests/test_config.py
git commit -m "Refuse a non-string target binding connection cleanly"
```

---

## Task 14: Contain offline handler input errors

**Files:**
- Modify: `scripts/team.py:570` (wrap the handler call)
- Modify: `scripts/teamlib/fingerprints.py:194-202` (guard the input read)
- Test: `scripts/tests/test_launchers.py`

`team.py snapshot --rows /nonexistent.json --out x` prints a `FileNotFoundError` traceback. `fingerprints.main` reads and parses `--rows` unguarded, and `team.py`'s dispatcher does not catch `OSError`, `json.JSONDecodeError` or `InventoryError`. Every other command in this CLI answers a bad input with a message on stderr and exit 2 or 3.

**Interfaces:**
- Consumes: `config.ConfigError` (existing).
- Produces: `team._offline(args) -> int` — same signature; input errors now surface as `ConfigError`.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_launchers.py`:

```python
class OfflineErrorContainmentTests(unittest.TestCase):
    def _run(self, argv):
        import io
        import contextlib

        import team

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = team.main(argv)
        return code, stderr.getvalue()

    def test_missing_rows_file_is_a_refusal_not_a_traceback(self):
        code, message = self._run(["snapshot", "--rows", "/nonexistent-rows.json", "--out", "/tmp/out.json"])
        self.assertIn(code, (2, 3))
        self.assertIn("nonexistent-rows.json", message)
        self.assertNotIn("Traceback", message)

    def test_malformed_rows_json_is_a_refusal(self):
        directory = Path(tempfile.mkdtemp(prefix="team-rows-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        rows = directory / "rows.json"
        rows.write_text("{not json", encoding="utf-8", newline="\n")
        code, message = self._run(["snapshot", "--rows", str(rows), "--out", str(directory / "out.json")])
        self.assertIn(code, (2, 3))
        self.assertNotIn("Traceback", message)

    def test_rows_document_without_rows_is_a_refusal(self):
        directory = Path(tempfile.mkdtemp(prefix="team-rows2-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        rows = directory / "rows.json"
        rows.write_text('{"topology": "separate"}', encoding="utf-8", newline="\n")
        code, message = self._run(["snapshot", "--rows", str(rows), "--out", str(directory / "out.json")])
        self.assertIn(code, (2, 3))
        self.assertNotIn("Traceback", message)
```

Confirm the module imports `shutil`, `tempfile` and `Path`; add any that are missing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_launchers -k OfflineErrorContainment -v 2>&1 | tail -25`
Expected: FAIL with `FileNotFoundError` escaping `team.main`

- [ ] **Step 3: Guard the input read in `fingerprints.main`**

In `scripts/teamlib/fingerprints.py`, replace the body of `main` from `data = json.loads(...)` with:

```python
    try:
        data = json.loads(Path(args.rows).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InventoryError(f"qualified inventory rows are unreadable: {args.rows}: {exc}") from exc
    snapshot(data, args.out)
    return 0
```

- [ ] **Step 4: Contain input errors in the dispatcher**

In `scripts/team.py`, replace:

```python
    result = handler(handler_args)
    return int(result or 0)
```

with:

```python
    try:
        result = handler(handler_args)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        # Offline handlers take explicit local inputs. A missing or malformed
        # one is a user error and must answer with the same structured refusal
        # every other command produces, not a traceback.
        raise ConfigError(f"{args.command} could not read its input: {exc}") from exc
    return int(result or 0)
```

Add `InventoryError` to the caught tuple in `main`, changing:

```python
    except (ConfigError, ControlStoreError, StateError, PatchError, ApexError, MigrationRunError, MigrationStoreError, DeployError, ReleaseError, RunbookError, CIError, AppCheckError, TreeError, BundleError) as exc:
```

to:

```python
    except (ConfigError, ControlStoreError, StateError, PatchError, ApexError, MigrationRunError, MigrationStoreError, DeployError, ReleaseError, RunbookError, CIError, AppCheckError, TreeError, BundleError, InventoryError) as exc:
```

`InventoryError` is already imported at the top of `team.py` from `teamlib.fingerprints`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_launchers -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 6: Confirm the reported case by hand**

Run: `cd /home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM && PYTHONPATH=scripts python3 scripts/team.py snapshot --rows /nonexistent.json --out /tmp/x.json; echo "exit=$?"`
Expected: a one-line message on stderr naming the file, then `exit=2`

- [ ] **Step 7: Run the full suite and lint**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5 && python3 -m ruff check scripts/`
Expected: `OK` then `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add scripts/team.py scripts/teamlib/fingerprints.py scripts/tests/test_launchers.py
git commit -m "Answer a bad offline input with a refusal instead of a traceback"
```

---

## Task 15: Make the inventory manifest CLOB byte-safe

**Files:**
- Modify: `scripts/teamlib/migration_store.py:239`
- Test: `scripts/tests/test_sql_metadata_store.py`

`record_inventory` serialises the manifest with `ensure_ascii=False`, and `read_inventories` reads it back in 900-character `DBMS_LOB.SUBSTR` chunks base64-encoded through `UTL_RAW.CAST_TO_RAW`/`UTL_ENCODE.BASE64_ENCODE`. In SQL those return `RAW`, capped at 2000 bytes. The other two CLOBs this module writes (`dependency_json`, `observation_json`) use `json.dumps`' default `ensure_ascii=True`, so 900 characters is at most 900 bytes and base64 yields 1200 — safe. The manifest is the one exception: a schema with a quoted non-ASCII object name makes a 900-character chunk up to 3600 bytes and the encode raises `ORA-06502`, after which the evidence can never be read back.

The digest is computed from the `Inventory` object, not from the serialised bytes, and `json.loads` of `\uXXXX` escapes reproduces the same dict, so switching to ASCII escaping round-trips identically. A manifest that is already pure ASCII is byte-identical either way, so the `DBMS_LOB.COMPARE` immutability check is unaffected for every manifest that could have been stored successfully until now.

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_sql_metadata_store.py`:

```python
class ManifestEncodingTests(unittest.TestCase):
    def test_manifest_json_is_ascii_so_900_characters_is_900_bytes(self):
        import inspect

        from teamlib.migration_store import SqlMigrationStore

        source = inspect.getsource(SqlMigrationStore.record_inventory)
        self.assertIn("manifest_json = json.dumps(", source)
        self.assertNotIn(
            "ensure_ascii=False",
            source,
            "read_inventories chunks the manifest 900 characters at a time and "
            "base64-encodes each chunk through SQL RAW, which is capped at 2000 "
            "bytes; non-ASCII characters overflow it",
        )

    def test_ascii_escaped_manifest_round_trips(self):
        import json

        from teamlib.fingerprints import inventory_from_manifest, inventory_from_rows

        inventory = inventory_from_rows(
            [{"logical_owner": "tables", "object_type": "TABLE", "object_name": "MITARBEITER_Ü", "definition": "x"}],
            schema_set_digest="a" * 64,
        )
        encoded = json.dumps(inventory.as_dict(), sort_keys=True, separators=(",", ":"))
        self.assertTrue(all(ord(char) < 128 for char in encoded))
        self.assertEqual(inventory_from_manifest(json.loads(encoded)).digest, inventory.digest)
```

- [ ] **Step 2: Run the tests to verify one fails**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_sql_metadata_store -k ManifestEncoding -v 2>&1 | tail -20`
Expected: `test_manifest_json_is_ascii_so_900_characters_is_900_bytes` FAILS; the round-trip test PASSES

- [ ] **Step 3: Escape the manifest to ASCII**

In `scripts/teamlib/migration_store.py`, replace line 239:

```python
        manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

with:

```python
        # read_inventories reads this CLOB back in 900-character chunks and
        # base64-encodes each one through UTL_RAW/UTL_ENCODE, whose SQL results
        # are RAW and capped at 2000 bytes. ASCII escaping keeps 900 characters
        # at 900 bytes, matching the other two CLOBs this module writes. The
        # digest is computed from the Inventory, not these bytes, so escaping
        # does not change identity.
        manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
```

- [ ] **Step 4: Run the tests to verify both pass**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_sql_metadata_store -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add scripts/teamlib/migration_store.py scripts/tests/test_sql_metadata_store.py
git commit -m "Keep the inventory manifest CLOB within the SQL RAW chunk budget"
```

---

## Task 16: Close two latent contract gaps

**Files:**
- Modify: `scripts/teamlib/state.py:356-375` (`save_capture` refuses the shape `load_capture` rejects)
- Modify: `scripts/teamlib/config.py:633-638` (`is_offline_command` prefix handling)
- Test: `scripts/tests/test_state.py`, `scripts/tests/test_config.py`

Neither of these fails today. `save_capture` accepts a tree mapping as `head` and then stores `head_commit = ""` unless a `head_commit` diagnostic overrides it — a shape `load_capture` unconditionally rejects. Every real caller supplies a commit or that diagnostic, so the rejected shape is unreachable; the fix makes the impossible state impossible to write rather than merely unwritten. `is_offline_command`'s script-prefix branch never strips anything (`"team.py".rsplit("/", 1)[-1]` is `"team.py"`, which is not an offline command), and the function has no caller outside its own test.

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `state.save_capture(...)` — same signature; now raises `StateError` when a tree `head` arrives with no `head_commit` diagnostic.
  - `config.is_offline_command(command: str) -> bool` — same signature; now handles a bare name, a `team.py <command>` string, and a path-prefixed `./scripts/team.py <command>` string.

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_state.py`:

```python
class CaptureHeadContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-capture-head-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="docker-demo", instance_id="FREE", db_name="FREEPDB1",
            service="freep1", session_user="DEMO", current_schema="DEMO",
            alias="checkout", workspace_id=5402650006222933, app_id=100,
            parsing_schema="DEMO", ownership_mode="shared", binding_digest="a" * 64,
        )

    def test_tree_head_without_a_head_commit_diagnostic_is_refused(self):
        with self.assertRaises(StateError) as raised:
            save_capture(self.target, {}, {}, {"application.apx": b"x"}, {}, {"kind": "x"}, root=self.root)
        self.assertIn("head_commit", str(raised.exception))

    def test_tree_head_with_a_head_commit_diagnostic_round_trips(self):
        recovery_id = save_capture(
            self.target, {}, {}, {"application.apx": b"x"}, {},
            {"kind": "export", "head_commit": "a" * 40}, root=self.root,
        )
        self.assertEqual(load_capture(self.target, recovery_id, root=self.root).head, "a" * 40)

    def test_commit_head_still_round_trips(self):
        recovery_id = save_capture(
            self.target, {}, {}, "b" * 40, {"application.apx": b"x"},
            {"kind": "apex-capture"}, root=self.root,
        )
        self.assertEqual(load_capture(self.target, recovery_id, root=self.root).head, "b" * 40)
```

This duplicates `StateTests.setUp` rather than subclassing it: subclassing a `TestCase` re-runs every parent test under the child's name. Add `load_capture` to the module's existing `from teamlib.state import (...)` list — `Target`, `Path`, `tempfile`, `unittest`, `StateError` and `save_capture` are already imported there.

Append to `scripts/tests/test_config.py`:

```python
class OfflineCommandPrefixTests(unittest.TestCase):
    def test_bare_command_is_recognised(self):
        self.assertTrue(is_offline_command("build-release"))

    def test_script_prefixed_command_is_recognised(self):
        for prefix in ("team.py", "./scripts/team.py", "scripts/team.py", "team.sh", "team.ps1"):
            with self.subTest(prefix=prefix):
                self.assertTrue(is_offline_command(f"{prefix} build-release --ref v1.0.0"))

    def test_online_command_is_not_offline_with_any_prefix(self):
        self.assertFalse(is_offline_command("export-app"))
        self.assertFalse(is_offline_command("./scripts/team.py export-app checkout"))

    def test_empty_input_is_not_offline(self):
        self.assertFalse(is_offline_command(""))
        self.assertFalse(is_offline_command("   "))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_state tests.test_config -k "CaptureHeadContract or OfflineCommandPrefix" -v 2>&1 | tail -25`
Expected: FAIL — `test_tree_head_without_a_head_commit_diagnostic_is_refused` raises nothing; `test_script_prefixed_command_is_recognised` returns `False`

- [ ] **Step 3: Refuse the unloadable capture shape**

In `scripts/teamlib/state.py`, replace the `head` handling block in `save_capture`:

```python
    if isinstance(head, Mapping):
        head_commit = ""
        head_records, _ = _file_records(state_root, head)
    elif isinstance(head, str) and head:
        head_commit = head
        head_records = []
    else:
        raise StateError("capture HEAD must be a commit or exact source tree")
    if isinstance(diagnostics, Mapping) and isinstance(diagnostics.get("head_commit"), str):
        head_commit = diagnostics["head_commit"]
```

with:

```python
    diagnostic_commit = (
        diagnostics.get("head_commit") if isinstance(diagnostics, Mapping) else None
    )
    if isinstance(head, Mapping):
        head_records, _ = _file_records(state_root, head)
        # load_capture requires a non-empty commit. Writing a record it can
        # never read back is worse than refusing here: the caller still holds
        # the tree and can name the commit it came from.
        if not isinstance(diagnostic_commit, str) or not diagnostic_commit:
            raise StateError(
                "a capture with an exact source tree as HEAD requires a head_commit diagnostic"
            )
        head_commit = diagnostic_commit
    elif isinstance(head, str) and head:
        head_records = []
        head_commit = diagnostic_commit if isinstance(diagnostic_commit, str) and diagnostic_commit else head
    else:
        raise StateError("capture HEAD must be a commit or exact source tree")
```

- [ ] **Step 4: Fix the offline-command prefix handling**

In `scripts/teamlib/config.py`, replace `is_offline_command` with:

```python
_LAUNCHER_NAMES = frozenset({"team.py", "team.sh", "team.ps1"})


def is_offline_command(command: str) -> bool:
    """Return whether a team command can run without loading database config.

    Accepts a bare command name, or a full invocation whose first token is a
    launcher -- with or without a directory prefix. The previous implementation
    stripped a path only from a token that already started with ``team.py``,
    which is exactly the token that needs no stripping, so every prefixed form
    reported False.
    """
    tokens = command.strip().split()
    if not tokens:
        return False
    first = tokens[0].replace("\\", "/").rsplit("/", 1)[-1]
    if first in _LAUNCHER_NAMES:
        if len(tokens) < 2:
            return False
        first = tokens[1]
    return first in OFFLINE_COMMANDS
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_state tests.test_config -v 2>&1 | tail -25`
Expected: PASS

- [ ] **Step 6: Run the full suite and lint**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5 && python3 -m ruff check scripts/`
Expected: `OK` then `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add scripts/teamlib/state.py scripts/teamlib/config.py scripts/tests/test_state.py scripts/tests/test_config.py
git commit -m "Refuse an unloadable capture head and recognise prefixed offline commands"
```

---

# Phase 5 — Retention and hygiene

---

## Task 17: Bound every generated working directory

**Files:**
- Modify: `scripts/teamlib/prune.py:21-23` and `prune_scratch`
- Test: `scripts/tests/test_prune.py`

`_CAPTURE_GLOBS` names only `apex-capture-*` and `apex-import-*` at the top of `scratch/`. Three other generators are unbounded:

- `deploy.py:39` and `deploy.py:104` create `scratch/deploy-capture-<uuid>` and `scratch/deploy-import-<uuid>`, each holding a complete APEX export tree.
- `control_store._run` and `migration_store._run` create `scratch/metadata/control-<uuid>` and `scratch/metadata/migration-<uuid>` on **every** metadata statement — `read_app_sync_state` alone runs several times per import or deploy.
- `masters.apex_component_resolver` defaults to `scratch/master-checks/<uuid>`, though `apex.py:468` and `deploy.py:97` pass `.sync-state/master-checks` instead. Directories under `.sync-state/` are retained evidence and are deliberately **out of scope** for a scratch pruner; only the `scratch/` default is pruned here.

`prune_scratch` currently reports success while all of these accumulate.

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `prune.prune_scratch(repo, *, keep=5, dry_run=False) -> dict[str, Any]` — same signature and same keys, now counting the additional directories.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_prune.py`:

```python
class WorkDirectoryCoverageTests(unittest.TestCase):
    def _scratch(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="team-prune-cover-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        scratch = root / "scratch"
        for name in (
            "apex-capture-1", "apex-import-1",
            "deploy-capture-1", "deploy-import-1",
            "master-checks/aaaa", "metadata/control-1", "metadata/migration-1",
        ):
            (scratch / name).mkdir(parents=True)
            (scratch / name / "metadata.sql").write_text("select 1 from dual;\n", encoding="utf-8", newline="\n")
        return root

    def test_deploy_and_metadata_directories_are_removed(self):
        root = self._scratch()
        report = prune_scratch(root, keep=0)
        self.assertGreaterEqual(report["removed"], 7)
        for name in (
            "deploy-capture-1", "deploy-import-1",
            "metadata/control-1", "metadata/migration-1", "master-checks/aaaa",
        ):
            self.assertFalse((root / "scratch" / name).exists(), name)

    def test_keep_still_retains_the_most_recent_captures(self):
        root = self._scratch()
        prune_scratch(root, keep=10)
        self.assertTrue((root / "scratch" / "apex-capture-1").exists())
        self.assertTrue((root / "scratch" / "deploy-capture-1").exists())

    def test_dry_run_removes_nothing(self):
        root = self._scratch()
        report = prune_scratch(root, keep=0, dry_run=True)
        self.assertEqual(report["removed"], 0)
        self.assertGreater(report["would_remove"], 0)
        self.assertTrue((root / "scratch" / "metadata" / "control-1").exists())
```

Confirm the module imports `shutil`, `tempfile`, `Path` and `prune_scratch`; add any that are missing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_prune -k WorkDirectoryCoverage -v 2>&1 | tail -25`
Expected: `test_deploy_and_metadata_directories_are_removed` FAILS — the deploy and metadata directories still exist

- [ ] **Step 3: Widen the globs and add the nested sweep**

In `scripts/teamlib/prune.py`, replace:

```python
_RUN_FILE_GLOBS = (".team-driver-*", ".team-payload-*", ".team-stdin-*", ".team-sqlcl-*")
_CAPTURE_GLOBS = ("apex-capture-*", "apex-import-*")
```

with:

```python
_RUN_FILE_GLOBS = (".team-driver-*", ".team-payload-*", ".team-stdin-*", ".team-sqlcl-*")
# One directory per captured or imported application, retained by age.
_CAPTURE_GLOBS = (
    "apex-capture-*", "apex-import-*", "deploy-capture-*", "deploy-import-*",
)
# One directory per SQLcl statement. These are transient by construction -- the
# evidence a recovery record depends on lives in .sync-state -- so they are
# removed regardless of `keep`. Recovery evidence under .sync-state/ is never
# touched by this command.
_TRANSIENT_WORK_GLOBS = (
    "metadata/control-*", "metadata/migration-*", "master-checks/*",
)
```

Then, in `prune_scratch`, insert immediately after the `run_files` list comprehension:

```python
    transient = [
        path
        for pattern in _TRANSIENT_WORK_GLOBS
        for path in scratch.glob(pattern)
        if path.is_dir() and not path.is_symlink() and str(path.resolve()) not in referenced
    ]
```

Change the `dry_run` return's `would_remove` from:

```python
            "would_remove": len(doomed) + len(run_files),
```

to:

```python
            "would_remove": len(doomed) + len(run_files) + len(transient),
```

And insert into the removal loop, after the `for path in doomed:` block and before the `for path in run_files:` block:

```python
    for path in transient:
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_prune -v 2>&1 | tail -25`
Expected: PASS

- [ ] **Step 5: Update the module docstring**

Replace the docstring at the top of `scripts/teamlib/prune.py` with:

```python
"""Bounded retention for scratch working directories and SQLcl run files.

scratch/ holds one full application export per capture or deployment, one
directory per SQLcl statement issued to the metadata target, and four files per
SQLcl process. Retention is deliberate for recent application evidence, so
captures are pruned by age while anything a recovery record still points at is
pinned. Per-statement metadata and master-check directories are transient by
construction and are removed regardless of age.

Evidence under .sync-state/ is durable and is never touched by this command.
"""
```

- [ ] **Step 6: Run the full suite and lint**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5 && python3 -m ruff check scripts/`
Expected: `OK` then `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add scripts/teamlib/prune.py scripts/tests/test_prune.py
git commit -m "Prune deploy and per-statement metadata working directories"
```

---

## Task 18: Remove dead code and align the launchers

**Files:**
- Modify: `scripts/teamlib/release.py:251` (drop the shadowing `import io`)
- Modify: `scripts/teamlib/app_checks.py:220,288` (drop the unused `content` parameter)
- Modify: `scripts/teamlib/live_inventory.py:176-179` (drop the no-op branch)
- Modify: `scripts/build_release.ps1` (match its siblings)
- Test: `scripts/tests/test_launchers.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `app_checks._result(alias, check, callback) -> CheckResult` — the `content` parameter is removed; its only call site is updated in the same task.

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_launchers.py`:

```python
class LauncherConsistencyTests(unittest.TestCase):
    def test_every_powershell_launcher_uses_the_same_shape(self):
        from pathlib import Path

        scripts = Path(__file__).resolve().parents[1]
        wrappers = sorted(
            path for path in scripts.glob("*.ps1")
            if path.name not in {"team.ps1", "apply_release.ps1"}
        )
        self.assertTrue(wrappers, "expected thin PowerShell wrappers beside team.ps1")
        for path in wrappers:
            with self.subTest(script=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("$MyInvocation.MyCommand.Path", text)
                self.assertIn("scripts/team.ps1", text)
                self.assertNotIn("scripts\\team.ps1", text)
                self.assertTrue(
                    text.rstrip().endswith("exit $LASTEXITCODE"),
                    "a wrapper must propagate the exit code unconditionally",
                )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_launchers -k LauncherConsistency -v 2>&1 | tail -20`
Expected: FAIL on `build_release.ps1` — it uses `$PSScriptRoot`, a backslash path, and a conditional exit

- [ ] **Step 3: Align the PowerShell wrapper**

Replace the whole of `scripts/build_release.ps1` with:

```powershell
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
& (Join-Path $repoRoot 'scripts/team.ps1') build-release @args
exit $LASTEXITCODE
```

- [ ] **Step 4: Drop the shadowing import**

In `scripts/teamlib/release.py`, delete the line `import io` inside the `with tarfile.open(...)` loop (line 251). `io` is already imported at module scope on line 7 and the loop body uses `io.BytesIO(data)` either way.

- [ ] **Step 5: Drop the unused parameter**

In `scripts/teamlib/app_checks.py`, change the signature:

```python
def _result(alias: str, check: Mapping[str, Any], callback: Callable[..., Any] | None, content: Any) -> CheckResult:
```

to:

```python
def _result(alias: str, check: Mapping[str, Any], callback: Callable[..., Any] | None) -> CheckResult:
```

and update the only call site, replacing:

```python
        for check in declaration["checks"]:
            kind = str(check["kind"])
            content = check.get("sql") if kind == "select" else check.get("steps")
            results.append(_result(alias, check, _runner(replay_target, kind), content))
```

with:

```python
        for check in declaration["checks"]:
            kind = str(check["kind"])
            results.append(_result(alias, check, _runner(replay_target, kind)))
```

- [ ] **Step 6: Drop the no-op branch**

In `scripts/teamlib/live_inventory.py`, replace:

```python
    if target.environment == "production":
        operation = "read"
    else:
        operation = "read"
```

with:

```python
    # A schema inventory is read-only for every classification; production is
    # not a special case here, it is only refused a write elsewhere.
    operation = "read"
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `PYTHONPATH=scripts python3 -m unittest tests.test_launchers -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 8: Run the full suite and lint**

Run: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5 && python3 -m ruff check scripts/`
Expected: `OK` then `All checks passed!`

- [ ] **Step 9: Verify the template's own gates**

Run:

```bash
cd /home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM && for script in scripts/*.sh ci/provisioners/*.sh; do bash -n "$script" || echo "SYNTAX FAIL: $script"; done && find targets -name '*.json' -print0 | xargs -0 -n1 python3 -m json.tool >/dev/null && echo "json ok" && (! grep -RIl $'\r' --include='*.apx' --include='*.sql' --include='*.json' --include='*.sh' --include='*.ps1' . ) && echo "no CR"
```

Expected: `json ok` then `no CR`, with no `SYNTAX FAIL` lines

- [ ] **Step 10: Commit**

```bash
git add scripts/teamlib/release.py scripts/teamlib/app_checks.py scripts/teamlib/live_inventory.py scripts/build_release.ps1 scripts/tests/test_launchers.py
git commit -m "Remove dead code and align the build-release launcher"
```

---

## Final Verification

- [ ] **Step 1: Full suite**

Run: `cd /home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM && PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -t scripts -q 2>&1 | tail -5`
Expected: `OK` with at least 235 + the ~40 tests added by this plan

- [ ] **Step 2: Lint**

Run: `python3 -m ruff check scripts/`
Expected: `All checks passed!`

- [ ] **Step 3: Re-run every reproduction from the review**

Run:

```bash
cd /home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM && PYTHONPATH=scripts python3 - <<'PY'
from pathlib import Path
from teamlib.sqlcl import SqlclError, _assert_production_read_only
from teamlib.migration_bundle import BundleError, _assert_controls, _comment_directives

def refused(fn, *args):
    try:
        fn(*args)
        return "ALLOWED"
    except (SqlclError, BundleError) as exc:
        return f"refused ({exc})"

print("prod inline DROP :", refused(_assert_production_read_only, "SELECT 1 FROM dual; DROP TABLE audit_log;"))
print("prod after SET   :", refused(_assert_production_read_only, "SET HEADING OFF\nDROP TABLE audit_log;"))
print("inventory        :", refused(_assert_production_read_only, Path("scripts/sql/schema_inventory.sql").read_text()))
print("mig inline HOST  :", refused(_assert_controls, "SELECT 1 FROM dual; HOST rm -rf /;"))
print("mig inline @     :", refused(_assert_controls, "SELECT 1 FROM dual; @malicious.sql;"))
print("q-quote directive:", _comment_directives("SELECT q'[It's a trap -- depends-on: forged]' FROM dual;"))
PY
```

Expected:

```text
prod inline DROP : refused (...)
prod after SET   : refused (...)
inventory        : ALLOWED
mig inline HOST  : refused (...)
mig inline @     : refused (...)
q-quote directive: []
```

- [ ] **Step 4: Confirm the working tree is clean and every task is committed**

Run: `git status --short && git log --oneline -18`
Expected: no modified files; 18 commits, one per task, on a feature branch

---

## Notes for the Executor

- **Test module names.** The suite is discovered with `-s scripts/tests -t scripts`, so modules import as `tests.test_<name>`, not `scripts.tests.test_<name>`. Every `Run:` line in this plan already uses the working form.
- **Test doubles and new keyword arguments.** Task 6 adds `timeout=` to `deploy.py`'s runner calls. Any stub runner in the test suite declared without `**kwargs` will start failing with `TypeError: runner() got an unexpected keyword argument 'timeout'`. Grep before assuming a failure is a real regression.
- **Task ordering matters within Phase 1 only.** Tasks 3 and 4 both import from `sql_text` and cannot land before Tasks 1 and 2. Phases 2–5 are independent of each other and of Phase 1; they may be executed in any order or in parallel worktrees.
- **Do not weaken a guard to make a test pass.** If a change in Phase 1 makes an existing test fail because a construct is now refused that was previously accepted, the correct response is to confirm the construct really is unsafe and update the test — not to widen the allowlist. If it is genuinely safe (a false positive on real Oracle SQL), name it explicitly in the allowlist and add a test for both directions.
