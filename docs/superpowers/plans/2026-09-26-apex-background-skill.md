# APEX Background Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `apex-background` agent skill that tells developers and coding agents what APEX session, session state, bind variables, and substitution strings are available to PL/SQL that runs outside an interactive page request: automations, workflow activities, human task actions (standalone and inside workflows), and background execution chains. Every claim is backed by a probe run on APEX 26.1.

**Architecture:** Discovery first, documentation second. A self-contained probe ships with the skill under `.agents/skills/apex-background/probe/`:

- database objects (run/log tables, a package specification, and a separately
  compiled package body that install and finish can verify);
- an APEXlang probe application (app 9901), built from SQLcl's bundled starter app plus one component per background context;
- a Bash runner.

Each context calls the capture package, which evaluates a fixed list of session probes in its own exception handler. Separate components record bind variables, so an unsupported bind cannot hide the session probes. Each component writes a `CONTEXT_COMPLETE` marker only after its final bind probe; the finisher waits for expected marker counts rather than treating any row as completion. After disabling its schedule and completing its tasks, the finisher refreshes and validates the package body before it checks and completes the run. It exports logs and faults even when orchestration fails and leaves an incomplete run active for a retry or confirmed cleanup. A tested Python renderer turns all logged observations into `findings-apex-26.1.md`, and `SKILL.md` is written only from that matrix. The canonical skill lives in `.agents/skills/apex-background/`; a byte-identical `SKILL.md` in `.claude/skills/apex-background/` lets Claude Code discover it.

**Tech Stack:** Oracle APEX 26.1.4, APEXlang, SQLcl 26.2, PL/SQL, Bash, Python 3.10+ standard library, `unittest`, the built-in browser pane (for one page submit).

**Spec:** This plan is the spec. User request (2026-09-26): "I need to make some extra skills for Oracle APEX to be added to this repo. Let's start with [call this the APEX background skill, because background actions do not have APEX sessions, I think, except for tasks]: substitution strings discovery in the background processes, automation and workflow code activities, and APEX task actions and task actions in workflow activities."

**Execution status:** Tasks 1–6 are implemented and verified on this branch. Probe database phases were authorized by the user's “go ahead” instruction and each runner phase used its exact target confirmation. The user has since explicitly authorized committing this work and pushing it to `main`.

The original implementation passed 176 unittest cases, Ruff, ShellCheck, Bash
syntax, Python compileall, findings re-render comparison, and `git diff
--check`. After the final workspace-context guard edits, `bash -n` and
`git diff --check` passed. No live database recheck or test suite was run after
those last guards; the guard fails closed if the requested context is absent.

## Global Constraints

- The user's hypothesis ("background actions do not have APEX sessions, except for tasks") is a question to test, not a fact. `SKILL.md` states only what the probe observed and marks every other statement `not verified on 26.1`.
- Verified target: APEX `26.1.4`, SQLcl `26.2.2`, database `FREEPDB1`, saved connection `docker-demo`, workspace `DEMO`, parsing schema `DEMO`. Record these in the findings header.
- The probe writes to the shared development database: objects named `APEX_BG_PROBE*` in the parsing schema, and application `9901`. Ask the user before each `install`, `start`, `finish`, and `uninstall` run. Never run the probe against staging or production.
- The probe app is template tooling, not a team application: it never goes under `apps/`, and it is imported with SQLcl directly instead of `scripts/team.sh publish`.
- APEX API signatures used below were verified on this database on 2026-09-26:
  - `apex_automation.execute(p_application_id, p_static_id, p_run_in_background BOOLEAN)` and `apex_automation.enable/disable(p_application_id, p_static_id)`;
  - `apex_human_task.create_task(...) RETURN NUMBER` and `apex_human_task.approve_task(p_task_id, p_autoclaim)`;
  - `apex_workflow.start_workflow(p_application_id, p_static_id, p_parameters, p_initiator, p_detail_pk, p_debug_level) RETURN NUMBER`;
  - `apex_session.create_session(p_app_id, p_page_id, p_username)` and `apex_application.do_substitutions(p_string) RETURN VARCHAR2`;
  - `apex_application_install.remove_application(p_application_id)`.
- APEXlang names below come from SQLcl's `apexlang_meta_data.json` and from exported demo apps 150 and 200. When `apex import` rejects a name, look it up with the Task 3 helper command. Do not guess.
- Skills use the frontmatter `name` / `description` format. `name` must equal the directory name.
- Do not commit or push without an explicit instruction.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `.agents/skills/apex-background/SKILL.md` | The skill (canonical) |
| `.claude/skills/apex-background/SKILL.md` | Byte-identical copy for Claude Code discovery |
| `.agents/skills/apex-background/findings-apex-26.1.md` | Rendered probe evidence the skill cites |
| `.agents/skills/apex-background/probe/install.sql` | Probe tables and `APEX_BG_PROBE` package |
| `.agents/skills/apex-background/probe/package-body.sql` | Reusable body compilation plus compiler-error check |
| `.agents/skills/apex-background/probe/uninstall.sql` | Removes probe objects |
| `.agents/skills/apex-background/probe/contexts.sql` | Starts a run and triggers SQLcl-driven contexts |
| `.agents/skills/apex-background/probe/finish.sql` | Waits for completion markers, approves tasks, disables the schedule, and exports evidence even on failure |
| `.agents/skills/apex-background/probe/app/` | APEXlang probe application source |
| `.agents/skills/apex-background/probe/app/shared-components/messages.apx` | Text message fixture for the official `APP_TEXT$...` substitution |
| `.agents/skills/apex-background/probe/run.sh` | `install`, `start`, `finish`, `report`, `uninstall` phases |
| `.agents/skills/apex-background/probe/render_findings.py` | CSV to Markdown matrix |
| `tests/test_apex_background_skill.py` | Renderer, probe layout, and skill contract tests |

---

### Task 1: Findings renderer

**Files:**
- Create: `.agents/skills/apex-background/probe/render_findings.py`, `tests/test_apex_background_skill.py`

**Interfaces:**
- Produces: `python3 render_findings.py <log.csv> <faults.csv> --apex <version> --database <name> > findings.md`.
  - `log.csv` has the header `CONTEXT_NAME,PROBE_NAME,PROBE_VALUE,PROBE_ERROR`; `faults.csv` has `SOURCE,NAME,DETAIL`. Both are SQLcl `SET SQLFORMAT CSV` output with double-quoted fields.
  - Output: a header with the versions, a Markdown table with one row per probe and one column per context (contexts in first-seen order), then a `## Faults` list. A cell is the value, `<null>`, `error: <message>`, or `-` when the context did not record that probe. If a task or repeated run records the same probe more than once in one context, preserve every observation by joining the cell values with `; ` in capture order.

- [x] **Step 1: Write the failing renderer tests**

Create `tests/test_apex_background_skill.py`:

```python
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "apex-background"
RENDERER = SKILL / "probe" / "render_findings.py"


class RenderFindingsTests(unittest.TestCase):
    def render(self, log: str, faults: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "log.csv"
            faults_path = Path(temporary) / "faults.csv"
            log_path.write_text(log, encoding="utf-8")
            faults_path.write_text(faults, encoding="utf-8")
            return subprocess.run(
                ["python3", str(RENDERER), str(log_path), str(faults_path), "--apex", "26.1.4", "--database", "FREEPDB1"],
                text=True, capture_output=True, check=False,
            )

    def test_matrix_has_one_column_per_context_in_first_seen_order(self) -> None:
        result = self.render(
            '"CONTEXT_NAME","PROBE_NAME","PROBE_VALUE","PROBE_ERROR"\n'
            '"SQLCL_APEX_SESSION","V(APP_SESSION)","1234",\n'
            '"SQLCL_APEX_SESSION","V(APP_SESSION)","1235",\n'
            '"WORKFLOW_START","V(APP_SESSION)","<null>",\n'
            '"SQLCL_APEX_SESSION","BIND :APEX$TASK_ID","",\n'
            '"WORKFLOW_START","DO_SUBSTITUTIONS(&APP_TITLE.)",,"ORA-06550: boom"\n',
            '"SOURCE","NAME","DETAIL"\n',
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("APEX 26.1.4", result.stdout)
        self.assertIn("| Probe | SQLCL_APEX_SESSION | WORKFLOW_START |", lines)
        self.assertIn("| V(APP_SESSION) | 1234; 1235 | <null> |", lines)
        self.assertIn("| BIND :APEX$TASK_ID | <null> | - |", lines)
        self.assertIn("| DO_SUBSTITUTIONS(&APP_TITLE.) | - | error: ORA-06550: boom |", lines)
        self.assertIn("No faults recorded.", result.stdout)

    def test_pipes_in_values_are_escaped_and_faults_listed(self) -> None:
        result = self.render(
            '"CONTEXT_NAME","PROBE_NAME","PROBE_VALUE","PROBE_ERROR"\n'
            '"A","P","x|y",\n',
            '"SOURCE","NAME","DETAIL"\n"workflow activity","binds-start","faulted: ORA-01008"\n',
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| P | x\\|y |", result.stdout)
        self.assertIn("- workflow activity `binds-start`: faulted: ORA-01008", result.stdout)

    def test_missing_header_is_rejected(self) -> None:
        result = self.render('"A","B"\n', '"SOURCE","NAME","DETAIL"\n')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected CSV header", result.stderr)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run the tests and confirm they fail**

Run: `python3 -m unittest tests.test_apex_background_skill -v`
Expected: FAIL, `can't open file ... render_findings.py`.

- [x] **Step 3: Implement the renderer**

Create `.agents/skills/apex-background/probe/render_findings.py`:

```python
#!/usr/bin/env python3
"""Render APEX background probe CSV output as a Markdown findings matrix."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


LOG_HEADER = ["CONTEXT_NAME", "PROBE_NAME", "PROBE_VALUE", "PROBE_ERROR"]
FAULT_HEADER = ["SOURCE", "NAME", "DETAIL"]


def read_csv(path: Path, header: list[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        rows = list(csv.reader(source))
    if not rows or rows[0] != header:
        raise ValueError(f"unexpected CSV header in {path}: {rows[0] if rows else 'empty file'}")
    return [dict(zip(header, row + [""] * (len(header) - len(row)))) for row in rows[1:] if row]


def cell(row: dict[str, str]) -> str:
    if row["PROBE_ERROR"]:
        text = f"error: {row['PROBE_ERROR']}"
    else:
        text = row["PROBE_VALUE"] or "<null>"
    return text.replace("|", "\\|").replace("\n", " ")


def render(log: list[dict[str, str]], faults: list[dict[str, str]], apex: str, database: str) -> str:
    contexts: list[str] = []
    probes: list[str] = []
    values: dict[tuple[str, str], str] = {}
    for row in log:
        if row["CONTEXT_NAME"] not in contexts:
            contexts.append(row["CONTEXT_NAME"])
        if row["PROBE_NAME"] not in probes:
            probes.append(row["PROBE_NAME"])
        values[(row["PROBE_NAME"], row["CONTEXT_NAME"])] = cell(row)
    lines = [
        "# APEX background probe findings",
        "",
        f"Observed on APEX {apex}, database {database}. Generated by probe/render_findings.py;",
        "do not edit by hand. `<null>` means the probe returned NULL; `-` means the context",
        "did not record that probe.",
        "",
        "| Probe | " + " | ".join(contexts) + " |",
        "| --- | " + " | ".join("---" for _ in contexts) + " |",
    ]
    for probe in probes:
        lines.append("| " + probe + " | " + " | ".join(values.get((probe, c), "-") for c in contexts) + " |")
    lines += ["", "## Faults", ""]
    if faults:
        lines += [f"- {f['SOURCE']} `{f['NAME']}`: {f['DETAIL']}" for f in faults]
    else:
        lines.append("No faults recorded.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_csv", type=Path)
    parser.add_argument("faults_csv", type=Path)
    parser.add_argument("--apex", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args(argv)
    try:
        log = read_csv(args.log_csv, LOG_HEADER)
        faults = read_csv(args.faults_csv, FAULT_HEADER)
    except (OSError, ValueError) as exc:
        print(f"render error: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(render(log, faults, args.apex, args.database))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 4: Run the tests, lint, and commit**

Run: `python3 -m unittest tests.test_apex_background_skill -v && .venv/bin/ruff check .agents tests`
Expected: PASS and `All checks passed!`.

```bash
git add .agents/skills/apex-background/probe/render_findings.py tests/test_apex_background_skill.py
git commit -m "feat: add APEX background probe findings renderer"
```

---

### Task 2: Probe database objects

**Files:**
- Create: `.agents/skills/apex-background/probe/install.sql`, `.agents/skills/apex-background/probe/package-body.sql`, `.agents/skills/apex-background/probe/uninstall.sql`
- Modify: `tests/test_apex_background_skill.py`

**Interfaces:**
- Produces, in the probe app's parsing schema:
  - tables `APEX_BG_PROBE_RUN(run_id, label, run_state, active_slot, started_at)` and `APEX_BG_PROBE_LOG(log_id, run_id, context_name, probe_name, probe_value, probe_error, captured_at)`;
  - `run_state` is `RUNNING` or `COMPLETE`; a unique virtual `active_slot` permits only one active run. `started_at` retains the database time zone for run-scoped automation log checks;
  - `complete_run` selects the active run ID before updating the run row, avoiding a query of the mutating table from inside the update predicate;
  - package `APEX_BG_PROBE` with `start_run`, `current_run`, `complete_run`, `capture`, `capture_bind`, `record_background_execution`, `record_context_complete`, and `record_issue` procedures/functions.
- Every write is an autonomous transaction, so rows survive a failing component. `capture` records these probe names, which later tasks and the skill rely on:
  - session state: `V(APP_ID)`, `V(APP_PAGE_ID)`, `V(APP_SESSION)`, `V(APP_USER)`, `V(APP_ALIAS)`, `V(WORKSPACE_ID)`, `V(PROBE_APP_ITEM)`, `V(P1_PROBE_ITEM)`;
  - globals: `APEX_APPLICATION.G_FLOW_ID`, `APEX_APPLICATION.G_FLOW_STEP_ID`, `APEX_APPLICATION.G_INSTANCE`, `APEX_APPLICATION.G_USER`;
  - session context: `SYS_CONTEXT(APEX$SESSION,APP_SESSION)`, `SYS_CONTEXT(APEX$SESSION,APP_USER)`, `SYS_CONTEXT(APEX$SESSION,WORKSPACE_ID)`;
  - substitutions: the official background-relevant built-ins from the inventory below, plus `DO_SUBSTITUTIONS(&PROBE_SUBST.)`, `DO_SUBSTITUTIONS(&PROBE_APP_ITEM.)`, and `DO_SUBSTITUTIONS(&P1_PROBE_ITEM.)`;
  - direct APEX globals for official names without `&` syntax: `G_FLOW_SCHEMA_OWNER`, `G_PROXY_SERVER`, `G_SYSDATE`, and `V(SYSDATE_YYYYMMDD)`;
  - database session: `USERENV SESSION_USER`, `USERENV CURRENT_SCHEMA`, `USERENV MODULE`, `USERENV ACTION`, `USERENV CLIENT_IDENTIFIER`, `USERENV CLIENT_INFO`, `USERENV BG_JOB_ID`, `USERENV SID`, and `SESSIONTIMEZONE`.
- `capture_bind` records `BIND :<name>`. `record_background_execution` captures the current execution ID/state using `APEX_BACKGROUND_PROCESS.GET_CURRENT_EXECUTION`; the finisher polls that ID using `GET_EXECUTION` and records failed/aborted status and its last status message. `record_context_complete` logs `CONTEXT_COMPLETE=OK`; call it after the final bind probe in each component. `record_issue` stores an orchestration failure under the active run without hiding the rest of the evidence.

An install retry may resume the app import only when all four existing SQL objects are valid and carry the probe ownership markers. A partial or unmarked object set fails closed and can be removed only by the separately confirmed uninstall phase. Uninstall accepts already-missing owned objects and continues dropping the remaining individually marked objects, so a partial cleanup can be retried safely.

### Official APEX substitution inventory (26.1)

Use Oracle's [Using Built-in Substitution Strings](https://docs.oracle.com/en/database/oracle/apex/26.1/htmdb/using-available-built-in-substitution-strings.html) as the complete general built-in catalog. The plan must account for every listed name below, including names that do not apply to background PL/SQL:

```text
APEX_CSP_DISPLAY_NONE, APEX_FILES, APEX$ROW_NUM, APEX$ROW_SELECTOR, APEX$ROW_STATUS,
APP_ID, APP_ALIAS, APP_AJAX_X01, APP_AJAX_X02, APP_AJAX_X03, APP_AJAX_X04, APP_AJAX_X05,
APP_AJAX_X06, APP_AJAX_X07, APP_AJAX_X08, APP_AJAX_X09, APP_AJAX_X10, APP_BUILDER_SESSION,
APP_DATE_TIME_FORMAT, APP_FILES, APP_NLS_DATE_FORMAT, APP_NLS_TIMESTAMP_FORMAT,
APP_NLS_TIMESTAMP_TZ_FORMAT, APP_PAGE_ALIAS, APP_PAGE_ID, APP_REGION_DOM_ID, APP_REGION_ID,
APP_REGION_STATIC_ID (deprecated), APP_REQUEST_DATA_HASH, APP_SESSION, SESSION (APP_SESSION alias), APP_SESSION_VISIBLE,
APP_TEXT$Message_Name, APP_TEXT$Message_Name$Lang, APP_TITLE, APP_UNIQUE_PAGE_ID, APP_USER,
APP_VERSION, AUTHENTICATED_URL_PREFIX, BROWSER_LANGUAGE, CURRENT_PARENT_TAB_TEXT, DEBUG,
DEFAULT_THEME_FILES, HOME_LINK, JET_BASE_DIRECTORY, JET_CSS_DIRECTORY, JET_JS_DIRECTORY,
LOGIN_URL, LOGOUT_URL, MAIN_APP_ID, OWNER, PRINTER_FRIENDLY, PROXY_SERVER, PUBLIC_URL_PREFIX,
REQUEST, SCHEMA OWNER, SQLERRM, SYSDATE_YYYYMMDD, THEME_DB_FILES, THEME_FILES,
WORKSPACE_FILES, WORKSPACE_ID
```

Probe each general built-in whose documented syntax includes `&NAME.` through `APEX_APPLICATION.DO_SUBSTITUTIONS` in every captured context. Expand `APP_AJAX_X01` through `APP_AJAX_X10` individually. The application includes `PROBE_MESSAGE` so `APP_TEXT$PROBE_MESSAGE` and `APP_TEXT$PROBE_MESSAGE$EN` test an actual text message. Also probe the documented legacy aliases `APP_IMAGES`, `IMAGE_PREFIX`, `THEME_DB_IMAGES`, `THEME_IMAGES`, and `WORKSPACE_IMAGE` because the 26.1 guide says they remain supported.

The probe and eventual skill must clearly mark these documented scope limits rather than present them as universal background values:

- `APEX_CSP_DISPLAY_NONE`, `APP_VERSION`, `DEFAULT_THEME_FILES`, `JET_BASE_DIRECTORY`, `JET_CSS_DIRECTORY`, `JET_JS_DIRECTORY`, `OWNER`, `SQLERRM`, `THEME_DB_FILES`, and `THEME_FILES` are template-only (`#NAME#`) values. `APP_REGION_*`, `CURRENT_PARENT_TAB_TEXT`, `APP_PAGE_*`, `APP_AJAX_X01` through `APP_AJAX_X10`, `APP_REQUEST_DATA_HASH`, `APP_UNIQUE_PAGE_ID`, and `REQUEST` are page, region, browser-request, or row-processing values; record the observed result, but do not claim they are meaningful in a background context.
- `APEX$ROW_NUM`, `APEX$ROW_SELECTOR`, and `APEX$ROW_STATUS` are for tabular form or grid row processing. Probe them as official names and explain why background rows ordinarily have no current form row.
- Oracle documents `SESSION` as a short alias for `APP_SESSION`; capture both forms.
- `PROXY_SERVER` and `SCHEMA OWNER` are headings in the official catalog, but their documented PL/SQL forms are `APEX_APPLICATION.G_PROXY_SERVER` and `APEX_APPLICATION.G_FLOW_SCHEMA_OWNER`; `SYSDATE_YYYYMMDD` is documented through `V`, a bind, or `G_SYSDATE`, not `&NAME.`. Capture these forms directly.
- `APP_NAME` is not in the 26.1 official built-in catalog. Use official `APP_TITLE` in probes and examples.

The three substitution-name catalogs used here are the general built-ins, workflow strings, and task strings linked above. Keep other background-context values distinct from substitutions: Oracle documents automation query columns as bind variables in [Understanding Key Automation Concepts](https://docs.oracle.com/en/database/oracle/apex/26.1/apxdc/understanding-key-automation-concepts.html), and describes background page execution as a private session with a copy of the page session state in [Understanding Background Page Processing](https://docs.oracle.com/en/database/oracle/apex/26.1/htmdb/understanding-background-page-processing.html). Record those values under their execution contexts; do not invent `APEX$AUTOMATION_*` or `APEX$BACKGROUND_*` substitution names without an Oracle reference.

### Background-specific workflow and task substitution strings

The Oracle 26.1 [Workflow Substitution Strings](https://docs.oracle.com/en/database/oracle/apex/26.1/htmdb/workflow-substitution-strings.html) and [Task Substitution Strings and Bind Variables](https://docs.oracle.com/en/database/oracle/apex/26.1/htmdb/substitution-strings-for-tasks.html) pages add context-specific catalogs. The workflow has six names: `APEX$WORKFLOW_ACTIVITY_ID`, `APEX$WORKFLOW_CREATED_ON`, `APEX$WORKFLOW_DETAIL_PK`, `APEX$WORKFLOW_ID`, `APEX$WORKFLOW_INITIATOR`, and `APEX$WORKFLOW_STATE`. The task has thirteen: `APEX$TASK_CREATED_ON`, `APEX$TASK_DUE_ON`, `APEX$TASK_ID`, `APEX$TASK_INITIATOR`, `APEX$TASK_MAX_RENEWAL_COUNT`, `APEX$TASK_OUTCOME`, `APEX$TASK_OWNER`, `APEX$TASK_PK`, `APEX$TASK_PREVIOUS_ID`, `APEX$TASK_RENEWAL_COUNT`, `APEX$TASK_STATE`, `APEX$TASK_SUBJECT`, and `APEX$TASK_TEXT`.

Probe all six workflow names in workflow activity code and all thirteen task values. Capture the twelve lifecycle values in task create/completion actions, with `APEX$TASK_OUTCOME` only on completion where Oracle documents it as populated. Oracle documents `APEX$TASK_TEXT` as the text for Add Comment, Request Information, and Submit Information actions. Capture it in an `updateComment` task-definition action by calling the documented `APEX_HUMAN_TASK.ADD_TASK_COMMENT` API for both the standalone task and the workflow-created task. Include each task ID in the probe name so repeated task operations remain identifiable in the rendered evidence. Request Information and Submit Information are outside this probe; the final skill must say they were not observed, rather than implying those operations have no value.

- [x] **Step 1: Add a failing layout test**

Append to `tests/test_apex_background_skill.py`:

```python
class ProbeLayoutTests(unittest.TestCase):
    def test_install_defines_every_probe_the_skill_relies_on(self) -> None:
        install = (SKILL / "probe" / "install.sql").read_text(encoding="utf-8")
        for probe in (
            "V(APP_SESSION)", "V(APP_USER)", "V(PROBE_APP_ITEM)", "V(P1_PROBE_ITEM)",
            "APEX_APPLICATION.G_INSTANCE", "SYS_CONTEXT(APEX$SESSION,APP_SESSION)",
            "DO_SUBSTITUTIONS(&PROBE_SUBST.)",
            "USERENV BG_JOB_ID", "USERENV MODULE",
        ):
            with self.subTest(probe=probe):
                self.assertIn(f"'{probe}'", install)
        self.assertIn("PRAGMA AUTONOMOUS_TRANSACTION", install)
        self.assertIn("capture_builtin_substitutions", install)
        self.assertIn("'APP_TITLE'", install)
        self.assertIn("SET DEFINE OFF", install)

    def test_uninstall_removes_every_installed_object(self) -> None:
        uninstall = (SKILL / "probe" / "uninstall.sql").read_text(encoding="utf-8")
        for statement in ("DROP PACKAGE apex_bg_probe", "DROP TABLE apex_bg_probe_log", "DROP TABLE apex_bg_probe_run"):
            with self.subTest(statement=statement):
                self.assertIn(statement, uninstall)
```

Run: `python3 -m unittest tests.test_apex_background_skill -v`
Expected: the two new tests ERROR with `FileNotFoundError`.

- [x] **Step 2: Write `install.sql`**

Create the two probe tables and `APEX_BG_PROBE` package. The run table stores
`run_state` (`RUNNING` or `COMPLETE`), a unique virtual active-run slot, and a
time-zone-aware `started_at`. Each log write is autonomous. The package exposes
`start_run`, `current_run`, `complete_run`, `capture`, `capture_bind`,
`record_context_complete`, and `record_issue`; each individual session probe
catches and records its own exception.

Mark both tables and the package body with `APEX_BG_PROBE_OWNER_V1`. A fresh
install creates all four SQL objects. A retry may skip object creation and
resume APEX import only when all four objects are valid and the two table
comments plus package body marker match. Partial, invalid, or unmarked objects
must fail closed and remain recoverable through the separately confirmed
uninstall phase.

- [x] **Step 3: Write `uninstall.sql`**

Validate ownership before dropping anything. Check each existing table against
its table comment and the package spec/body against the body marker. Missing
objects are acceptable so an interrupted uninstall can be retried; a present
object without its expected marker blocks cleanup. Drop the package, log table,
and run table in dependency order, and retain local `.run` evidence.

- [x] **Step 4: Run the tests and commit**

Run: `python3 -m unittest tests.test_apex_background_skill -v`
Expected: PASS.

```bash
git add .agents/skills/apex-background/probe/install.sql .agents/skills/apex-background/probe/uninstall.sql tests/test_apex_background_skill.py
git commit -m "feat: add APEX background probe database objects"
```

---

### Task 3: Probe application source and runner

**Files:**
- Create: `.agents/skills/apex-background/probe/app/**` (from the SQLcl starter app plus the files below), `.agents/skills/apex-background/probe/run.sh`, `.agents/skills/apex-background/probe/contexts.sql`, `.agents/skills/apex-background/probe/finish.sql`
- Modify: `tests/test_apex_background_skill.py`

**Interfaces:**
- Consumes: `APEX_BG_PROBE` package from Task 2.
- Produces:
  - Application 9901, alias `APEX-BG-PROBE`, authentication `noAuth`, application substitution `PROBE_SUBST=probe-subst-value`, application item `PROBE_APP_ITEM`.
  - Components, with the context name each records:

    | Component (static ID) | Context |
    | --- | --- |
    | automation `bg-probe-on-demand` | `AUTOMATION_ON_DEMAND` |
    | automation `bg-probe-on-demand-bg` | `AUTOMATION_ON_DEMAND_BACKGROUND` |
    | automation `bg-probe-scheduled` (schedule disabled in source) | `AUTOMATION_SCHEDULED` |
    | task definition `bg-probe-task`, `create` event | `TASK_ACTION_CREATE` |
    | task definition `bg-probe-task`, `complete` event with outcome `approved` | `TASK_ACTION_COMPLETE` |
    | task definition `bg-probe-task`, Add Comment operation | `TASK_ACTION_COMMENT` |
    | workflow `bg-probe-workflow`, at start | `WORKFLOW_START` |
    | workflow `bg-probe-workflow`, after a one-minute Wait | `WORKFLOW_AFTER_WAIT` |
    | workflow `bg-probe-workflow`, after its human task completes | `WORKFLOW_AFTER_TASK` |
    | page 1 process, foreground | `PAGE_PROCESS_FOREGROUND` |
    | page 1 execution chain, background | `EXECUTION_CHAIN_BACKGROUND` |

  - The SQLcl-driven contexts are `SQLCL_NO_SESSION` and `SQLCL_APEX_SESSION`.
  - `run.sh <install|start|finish|report|uninstall> <sqlcl-connection> <workspace> <parsing-schema>`.

- [x] **Step 1: Add a failing app layout test**

Append to `ProbeLayoutTests`:

```python
    def test_probe_app_defines_every_background_context(self) -> None:
        app = SKILL / "probe" / "app"
        source = "\n".join(p.read_text(encoding="utf-8") for p in app.rglob("*.apx"))
        for context in (
            "AUTOMATION_ON_DEMAND'", "AUTOMATION_ON_DEMAND_BACKGROUND'", "AUTOMATION_SCHEDULED'",
            "TASK_ACTION_CREATE'", "TASK_ACTION_COMPLETE'", "WORKFLOW_START'", "WORKFLOW_AFTER_WAIT'",
            "TASK_ACTION_COMMENT'", "WORKFLOW_AFTER_TASK'", "PAGE_PROCESS_FOREGROUND'", "EXECUTION_CHAIN_BACKGROUND'",
        ):
            with self.subTest(context=context):
                self.assertIn("'" + context, source)
        application = (app / "application.apx").read_text(encoding="utf-8")
        self.assertTrue(application.startswith("app APEX-BG-PROBE ("))
        self.assertIn("substitution PROBE_SUBST (", application)
        self.assertIn("scheme: @no-authentication", application)
        self.assertIn("scheduleStatus: disabled", source)
```

Run: `python3 -m unittest tests.test_apex_background_skill -v`
Expected: the new test FAILS (`application.apx` is missing).

- [x] **Step 2: Copy the SQLcl starter app**

```bash
PROBE=.agents/skills/apex-background/probe
SQLCL_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v sql)")")")"
SCRATCH_ZIP="$(mktemp -d)"
unzip -q -o "$SQLCL_HOME/lib/ext/apexlang-compiler.jar" apexlang.zip -d "$SCRATCH_ZIP"
unzip -q -o "$SCRATCH_ZIP/apexlang.zip" 'apexlangmeta/starter-app/*' -d "$SCRATCH_ZIP"
mkdir -p "$PROBE/app"
cp -R "$SCRATCH_ZIP/apexlangmeta/starter-app/." "$PROBE/app/"
rm -f "$PROBE/app/deployments/default.json"
sed -i 's/^app @APP_ALIAS@ (/app APEX-BG-PROBE (/; s/^    name: @APP_NAME@$/    name: APEX Background Probe/' "$PROBE/app/application.apx"
head -2 "$PROBE/app/application.apx"
```

Expected: `app APEX-BG-PROBE (` and `    name: APEX Background Probe`.

- [x] **Step 3: Switch to No Authentication and add the substitution string**

Append to `$PROBE/app/shared-components/authentications.apx`:

```text

authentication no-authentication (
    name: No Authentication
    type: noAuth
)
```

In `$PROBE/app/application.apx`, change `scheme: @oracle-apex-accounts` to `scheme: @no-authentication`, and insert this block before the final `)`, after the `runtime { ... }` block and a blank line:

```text

    substitution PROBE_SUBST (
        value {
            staticValue: probe-subst-value
        }
    )
```

Also add `globalization { translationMethod: textMessages }` and create `$PROBE/app/shared-components/messages.apx`:

```text
textMessage PROBE_MESSAGE (
    message {
        text: background-substitution-message
        language: en
    }
)
```

Create `$PROBE/app/shared-components/app-items.apx`:

```text
appItem PROBE_APP_ITEM (
    security {
        sessionStateProtection: unrestricted
    }
)
```

- [x] **Step 4: Add the three automations**

Create `$PROBE/app/shared-components/automations/bg-probe-on-demand.apx`:

````text
automation bg-probe-on-demand (
    name: BG Probe On Demand
    execution {
        type: onDemand
        actionsInitiatedOn: always
    }

    action capture (
        name: Capture session
        type: executeCode
        source {
            plsqlCode: apex_bg_probe.capture('AUTOMATION_ON_DEMAND');
        }
        execution {
            sequence: 10
        }
    )

    action binds (
        name: Capture binds
        type: executeCode
        source {
            plsqlCode:
                ```plsql
                begin
                    apex_bg_probe.capture_bind('AUTOMATION_ON_DEMAND', 'APP_ID', :APP_ID);
                    apex_bg_probe.capture_bind('AUTOMATION_ON_DEMAND', 'APP_USER', :APP_USER);
                    apex_bg_probe.capture_bind('AUTOMATION_ON_DEMAND', 'APP_SESSION', :APP_SESSION);
                    apex_bg_probe.capture_bind('AUTOMATION_ON_DEMAND', 'PROBE_APP_ITEM', :PROBE_APP_ITEM);
                end;
                ```
        }
        execution {
            sequence: 20
        }
    )
)
````

Create `bg-probe-on-demand-bg.apx` with the same content, but with identifier `bg-probe-on-demand-bg`, name `BG Probe On Demand Background`, and every `'AUTOMATION_ON_DEMAND'` replaced by `'AUTOMATION_ON_DEMAND_BACKGROUND'`.

Create `bg-probe-scheduled.apx` with the same content, but with identifier `bg-probe-scheduled`, name `BG Probe Scheduled`, every context replaced by `'AUTOMATION_SCHEDULED'`, and this `execution` block:

```text
    execution {
        type: scheduled
        scheduleExpression: FREQ=MINUTELY;INTERVAL=1
        scheduleStatus: disabled
        actionsInitiatedOn: always
    }
```

- [x] **Step 5: Add the task definition**

Create `$PROBE/app/shared-components/task-definitions/bg-probe-task.apx`:

````text
taskDefinition bg-probe-task (
    name: BG Probe Task
    type: approval
    task {
        subject: BG probe task
        initiatorCanComplete: true
    }

    parameter T_PROBE_PARAM (
        label: T_PROBE_PARAM
    )

    action on-create (
        name: Capture on create
        type: executeCode
        source {
            plsqlCode: apex_bg_probe.capture('TASK_ACTION_CREATE');
        }
        execution {
            onEvent: create
            sequence: 10
        }
    )

    action on-create-binds (
        name: Capture binds on create
        type: executeCode
        source {
            plsqlCode:
                ```plsql
                begin
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_CREATED_ON', :APEX$TASK_CREATED_ON);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_DUE_ON', :APEX$TASK_DUE_ON);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_ID', :APEX$TASK_ID);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_INITIATOR', :APEX$TASK_INITIATOR);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_MAX_RENEWAL_COUNT', :APEX$TASK_MAX_RENEWAL_COUNT);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_OWNER', :APEX$TASK_OWNER);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_PK', :APEX$TASK_PK);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_PREVIOUS_ID', :APEX$TASK_PREVIOUS_ID);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_RENEWAL_COUNT', :APEX$TASK_RENEWAL_COUNT);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_STATE', :APEX$TASK_STATE);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_SUBJECT', :APEX$TASK_SUBJECT);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'T_PROBE_PARAM', :T_PROBE_PARAM);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APP_USER', :APP_USER);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APP_SESSION', :APP_SESSION);
                end;
                ```
        }
        execution {
            onEvent: create
            sequence: 20
        }
    )

    action on-complete (
        name: Capture on complete
        type: executeCode
        source {
            plsqlCode: apex_bg_probe.capture('TASK_ACTION_COMPLETE');
        }
        execution {
            onEvent: complete
            outcome: approved
            sequence: 30
        }
    )

    action on-complete-binds (
        name: Capture binds on complete
        type: executeCode
        source {
            plsqlCode:
                ```plsql
                begin
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_CREATED_ON', :APEX$TASK_CREATED_ON);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_DUE_ON', :APEX$TASK_DUE_ON);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_ID', :APEX$TASK_ID);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_INITIATOR', :APEX$TASK_INITIATOR);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_MAX_RENEWAL_COUNT', :APEX$TASK_MAX_RENEWAL_COUNT);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_OUTCOME', :APEX$TASK_OUTCOME);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_OWNER', :APEX$TASK_OWNER);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_PK', :APEX$TASK_PK);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_PREVIOUS_ID', :APEX$TASK_PREVIOUS_ID);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_RENEWAL_COUNT', :APEX$TASK_RENEWAL_COUNT);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_STATE', :APEX$TASK_STATE);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_SUBJECT', :APEX$TASK_SUBJECT);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'T_PROBE_PARAM', :T_PROBE_PARAM);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APP_USER', :APP_USER);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APP_SESSION', :APP_SESSION);
                end;
                ```
        }
        execution {
            onEvent: complete
            outcome: approved
            sequence: 40
        }
    )

    action on-comment-text (
        name: Capture task comment text
        type: executeCode
        source {
            plsqlCode:
                ```plsql
                begin
                    apex_bg_probe.capture_bind(
                        'TASK_ACTION_COMMENT',
                        'APEX$TASK_TEXT task ' || TO_CHAR(:APEX$TASK_ID),
                        :APEX$TASK_TEXT);
                end;
                ```
        }
        execution {
            onEvent: updateComment
            sequence: 50
        }
        error {
            stopExecutionOnError: true
            logging: all
        }
    )

    participant (
        value {
            type: staticValue
            staticValue: PROBE_USER
        }
    )
)
````

- [x] **Step 6: Add the workflow**

Create `$PROBE/app/shared-components/workflows/bg-probe-workflow.apx`. Each `executeCode` activity follows the same shape; bind activities list the binds shown.

````text
workflow bg-probe-workflow (
    name: BG Probe Workflow
    title: BG Probe Workflow

    parameter P_PROBE_PARAM (
        label {
            label: P_PROBE_PARAM
        }
    )

    version v1 (
        settings {
            state: active
        }
        additionalData {
            type: sqlQuery
            sqlQuery: select 'additional-data-value' as PROBE_DATA from dual
        }

        activity start (
            name: Start
            type: workflowStart
            layout {
                sequence: 10
            }
            connection (
                name: Next
                activity {
                    to: @capture-start
                }
            )
        )

        activity capture-start (
            name: Capture at start
            type: executeCode
            source {
                plsqlCode: apex_bg_probe.capture('WORKFLOW_START');
            }
            layout {
                sequence: 20
            }
            connection (
                name: Next
                activity {
                    to: @binds-start
                }
            )
        )

        activity binds-start (
            name: Capture binds at start
            type: executeCode
            source {
                plsqlCode:
                    ```plsql
                    begin
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'APEX$WORKFLOW_ACTIVITY_ID', :APEX$WORKFLOW_ACTIVITY_ID);
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'APEX$WORKFLOW_CREATED_ON', :APEX$WORKFLOW_CREATED_ON);
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'APEX$WORKFLOW_ID', :APEX$WORKFLOW_ID);
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'APEX$WORKFLOW_DETAIL_PK', :APEX$WORKFLOW_DETAIL_PK);
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'APEX$WORKFLOW_INITIATOR', :APEX$WORKFLOW_INITIATOR);
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'APEX$WORKFLOW_STATE', :APEX$WORKFLOW_STATE);
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'P_PROBE_PARAM', :P_PROBE_PARAM);
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'PROBE_DATA', :PROBE_DATA);
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'APP_USER', :APP_USER);
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'APP_SESSION', :APP_SESSION);
                    end;
                    ```
            }
            layout {
                sequence: 30
            }
            connection (
                name: Next
                activity {
                    to: @wait
                }
            )
        )

        activity wait (
            name: Wait one minute
            type: wait
            timeout {
                timeoutType: static
                staticValue: PT1M
            }
            layout {
                sequence: 40
            }
            connection (
                name: Next
                activity {
                    to: @capture-after-wait
                }
            )
        )

        activity capture-after-wait (
            name: Capture after wait
            type: executeCode
            source {
                plsqlCode:
                    ```plsql
                    begin
                        apex_bg_probe.capture('WORKFLOW_AFTER_WAIT');
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_WAIT', 'APEX$WORKFLOW_ACTIVITY_ID', :APEX$WORKFLOW_ACTIVITY_ID);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_WAIT', 'APEX$WORKFLOW_CREATED_ON', :APEX$WORKFLOW_CREATED_ON);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_WAIT', 'APEX$WORKFLOW_DETAIL_PK', :APEX$WORKFLOW_DETAIL_PK);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_WAIT', 'APEX$WORKFLOW_ID', :APEX$WORKFLOW_ID);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_WAIT', 'APEX$WORKFLOW_INITIATOR', :APEX$WORKFLOW_INITIATOR);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_WAIT', 'APEX$WORKFLOW_STATE', :APEX$WORKFLOW_STATE);
                    end;
                    ```
            }
            layout {
                sequence: 50
            }
            connection (
                name: Next
                activity {
                    to: @probe-task
                }
            )
        )

        activity probe-task (
            name: Probe task
            type: humanTaskCreate
            humanTask {
                definition: @bg-probe-task
            }
            result {
                outcome: TASK_OUTCOME
            }
            layout {
                sequence: 60
            }
            connection (
                name: Next
                activity {
                    to: @capture-after-task
                }
            )
            parameter (
                name: @T_PROBE_PARAM
                value {
                    type: expression
                    plsqlExpression: 'from-workflow'
                }
            )
        )

        activity capture-after-task (
            name: Capture after task
            type: executeCode
            source {
                plsqlCode: apex_bg_probe.capture('WORKFLOW_AFTER_TASK');
            }
            layout {
                sequence: 70
            }
            connection (
                name: Next
                activity {
                    to: @binds-after-task
                }
            )
        )

        activity binds-after-task (
            name: Capture binds after task
            type: executeCode
            source {
                plsqlCode:
                    ```plsql
                    begin
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_TASK', 'APEX$WORKFLOW_ACTIVITY_ID', :APEX$WORKFLOW_ACTIVITY_ID);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_TASK', 'APEX$WORKFLOW_CREATED_ON', :APEX$WORKFLOW_CREATED_ON);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_TASK', 'APEX$WORKFLOW_DETAIL_PK', :APEX$WORKFLOW_DETAIL_PK);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_TASK', 'APEX$WORKFLOW_ID', :APEX$WORKFLOW_ID);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_TASK', 'APEX$WORKFLOW_INITIATOR', :APEX$WORKFLOW_INITIATOR);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_TASK', 'APEX$WORKFLOW_STATE', :APEX$WORKFLOW_STATE);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_TASK', 'TASK_OUTCOME', :TASK_OUTCOME);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_TASK', 'APP_USER', :APP_USER);
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_TASK', 'APP_SESSION', :APP_SESSION);
                    end;
                    ```
            }
            layout {
                sequence: 80
            }
            connection (
                name: Next
                activity {
                    to: @end
                }
            )
        )

        activity end (
            name: End
            type: workflowEnd
            layout {
                sequence: 90
            }
        )

        variable TASK_OUTCOME (
            label {
                label: TASK_OUTCOME
            }
        )

        participant (
            name: Owner
            value {
                type: staticValue
                staticValue: PROBE_USER
            }
        )
    )
)
````

- [x] **Step 7: Replace page 1 with the background-chain page**

Overwrite `$PROBE/app/pages/p00001-home.apx`:

```text
page 1 (
    name: Home
    alias: HOME
    title: Home
    appearance {
        pageTemplate: @/standard
        templateOptions: #DEFAULT#
    }

    region probe (
        name: Background probe
        title: Background probe
        type: staticContent
        layout {
            sequence: 10
            slot: body
        }
        appearance {
            template: @/standard
            templateOptions: #DEFAULT#
        }
    )

    pageItem P1_PROBE_ITEM (
        type: textField
        label {
            label: Probe item
        }
        layout {
            sequence: 10
            region: @probe
            slot: regionBody
        }
        default {
            type: static
            staticValue: page-item-value
        }
    )

    button run-probe (
        buttonName: RUN_PROBE
        label: Run probe
        layout {
            sequence: 20
            region: @probe
            slot: create
        }
        appearance {
            buttonTemplate: @/text
            hot: true
            templateOptions: #DEFAULT#
        }
    )

    process capture-foreground (
        name: Capture in foreground
        type: executeCode
        source {
            plsqlCode: apex_bg_probe.capture('PAGE_PROCESS_FOREGROUND');
        }
        execution {
            sequence: 10
            point: processing
        }
        serverSideCondition {
            whenButtonPressed: @run-probe
        }
    )

    process background-chain (
        name: Background chain
        type: executionChain
        backgroundExecution {
            runInBackground: true
        }
        execution {
            sequence: 20
            point: processing
        }
        serverSideCondition {
            whenButtonPressed: @run-probe
        }
    )

    process capture-background (
        name: Capture in background
        type: executeCode
        executionChain: @background-chain
        source {
            plsqlCode: apex_bg_probe.capture('EXECUTION_CHAIN_BACKGROUND');
        }
        execution {
            sequence: 30
        }
    )

    process binds-background (
        name: Capture binds in background
        type: executeCode
        executionChain: @background-chain
        source {
            plsqlCode:
                ```plsql
                begin
                    apex_bg_probe.capture_bind('EXECUTION_CHAIN_BACKGROUND', 'P1_PROBE_ITEM', :P1_PROBE_ITEM);
                    apex_bg_probe.capture_bind('EXECUTION_CHAIN_BACKGROUND', 'APP_USER', :APP_USER);
                    apex_bg_probe.capture_bind('EXECUTION_CHAIN_BACKGROUND', 'APP_SESSION', :APP_SESSION);
                end;
                ```
        }
        execution {
            sequence: 40
        }
    )
)
```

- [x] **Step 8: Write the SQLcl context and finish scripts**

Create `.agents/skills/apex-background/probe/contexts.sql`:

```sql
-- Arguments: application id, run label. Starts a run and triggers the SQLcl-driven contexts.
SET DEFINE ON
SET SERVEROUTPUT ON
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
DEFINE app_id = '&1'
DEFINE run_label = '&2'

DECLARE
  l_run_id NUMBER;
  l_task_id NUMBER;
  l_workflow_id NUMBER;
BEGIN
  apex_bg_probe.start_run('&&run_label');
  l_run_id := apex_bg_probe.current_run;
  apex_bg_probe.capture('SQLCL_NO_SESSION');

  apex_session.create_session(p_app_id => &&app_id, p_page_id => 1, p_username => 'PROBE_USER');
  apex_util.set_session_state('PROBE_APP_ITEM', 'app-item-from-sqlcl');
  apex_util.set_session_state('P1_PROBE_ITEM', 'page-item-from-sqlcl');
  apex_bg_probe.capture('SQLCL_APEX_SESSION');

  apex_automation.execute(p_application_id => &&app_id, p_static_id => 'bg-probe-on-demand', p_run_in_background => FALSE);
  apex_automation.execute(p_application_id => &&app_id, p_static_id => 'bg-probe-on-demand-bg', p_run_in_background => TRUE);
  apex_automation.enable(p_application_id => &&app_id, p_static_id => 'bg-probe-scheduled');

  l_task_id := apex_human_task.create_task(
    p_application_id => &&app_id, p_task_def_static_id => 'bg-probe-task', p_subject => 'BG probe standalone',
    p_initiator => 'PROBE_USER', p_initiator_can_complete => TRUE,
    p_parameters => apex_human_task.t_task_parameters(
      1 => apex_human_task.t_task_parameter(static_id => 'T_PROBE_PARAM', string_value => 'from-sqlcl-task')),
    p_detail_pk => TO_CHAR(l_run_id));
  DBMS_OUTPUT.PUT_LINE('standalone task ' || l_task_id);
  apex_human_task.add_task_comment(
    p_task_id => l_task_id,
    p_text => 'comment text from SQLcl task probe');
  l_workflow_id := apex_workflow.start_workflow(
    p_application_id => &&app_id,
      p_static_id => 'bg-probe-workflow',
      p_parameters => apex_workflow.t_workflow_parameters(
        1 => apex_workflow.t_workflow_parameter(
          static_id => 'P_PROBE_PARAM',
          value => apex_session_state.t_value(
            data_type => apex_session_state.c_data_type_varchar2,
            varchar2_value => 'from-sqlcl-workflow'))),
    p_initiator => 'PROBE_USER',
    p_detail_pk => TO_CHAR(l_run_id));
  DBMS_OUTPUT.PUT_LINE('workflow ' || l_workflow_id);
  COMMIT;
END;
/
PROMPT APEX_BG_PROBE_STARTED
EXIT SUCCESS COMMIT
```

Create `.agents/skills/apex-background/probe/finish.sql` using this execution contract:

- Wait on `CONTEXT_COMPLETE=OK` rows for the required context counts, including two task-create and two task-complete actions. A probe row by itself is not completion.
- Disable the schedule even when a wait, task comment, or approval fails. Record each orchestration failure through `APEX_BG_PROBE.RECORD_ISSUE` and continue to spool `log.csv` and `faults.csv`.
- For the execution chain, record the execution ID inside the background chain with `APEX_BACKGROUND_PROCESS.GET_CURRENT_EXECUTION`. Poll it from the finisher with `GET_EXECUTION` until `SUCCESS`, `FAILED`, or `ABORTED`; include `last_status_message` when failed.
- Scope workflow activities by app, workflow static ID, and the active run's detail key; scope automation messages by app, automation static ID, and run start time; scope tasks by app and run detail key.
- Scope tasks either by their run `DETAIL_PK` or by their `WORKFLOW_ID` linked to this app's probe workflow with the same run detail key. Workflow-created tasks can have a null task `DETAIL_PK`.
- Create the APEX session before disabling the schedule, because `APEX_AUTOMATION.DISABLE` requires the session's security-group context.
- Start each finish attempt with a marker. When exporting and validating finish issues, count only issue rows after that marker so a retry can succeed after a prior failed attempt; preserve earlier CSVs in local `.previous-*` files.
- Set `VERIFY OFF` before command-line substitution and fault spooling, so SQLcl's substitution echo does not contaminate `faults.csv`.
- After both CSV files are written, leave the run active and return failure if any required completion marker is missing, workflow/automation errors exist, tasks remain open, or this attempt recorded an issue. The files must remain available to `report`. Mark the run `COMPLETE` only after these checks pass.
- On retry, preserve previous local CSV/log files under unique `.previous-*` names before writing new output. Uninstall retains `.run` files so an operator can inspect or report the evidence.

The APEX 26.1 view columns were checked read-only as the probe schema. Keep the run-scope predicates and the `faults.csv` export; do not revert to broad app-wide fault queries.

- [x] **Step 9: Write the runner**

Create `.agents/skills/apex-background/probe/run.sh` and make it executable.
The runner accepts `install`, `start`, `finish`, `report`, and `uninstall`, plus
a saved SQLcl connection, APEX workspace, and parsing schema. Every database
phase reads the DEV profile from `.env`, checks the connected user/current
schema, workspace, application ID/alias, and object ownership, then requires an
interactive confirmation naming the exact target identity and phase.

Use only the validated DEV connection. Keep `.run/` private to the current
user; reject symlinked or non-regular output files and preserve previous
staging directories and evidence under unique names. If a failed install left
all probe objects valid and marked, resume only the app import; reject partial
or unmarked database objects. If a finish phase returns an error after export,
leave `log.csv` and `faults.csv` available for `report` and retry. Uninstall
removes the application only if its ID, alias, workspace, and parsing schema
match; then it removes only individually marked objects and keeps `.run/` for
inspection.

Add `.agents/skills/apex-background/probe/.run/` to `.gitignore`.

- [x] **Step 10: Run the layout tests and shellcheck**

Run: `python3 -m unittest tests.test_apex_background_skill -v && shellcheck -S warning .agents/skills/apex-background/probe/run.sh`
Expected: PASS, no warnings.

- [x] **Step 11: Install on docker-demo (database write; ask the user first)**

Run: `.agents/skills/apex-background/probe/run.sh install docker-demo DEMO DEMO`
Expected: `APEX_BG_PROBE_INSTALLED` and `Import successful.`

If `apex import` rejects a property, list the valid APEXlang names for that component type from SQLcl's own metadata, fix the source, and rerun `install`:

```bash
SQLCL_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v sql)")")")"
unzip -p "$SQLCL_HOME/lib/ext/apexlang-compiler.jar" apexlang_meta_data.json > /tmp/apexlang_meta.json
python3 - automation <<'PY'
import json, sys
d = json.load(open("/tmp/apexlang_meta.json"))
wanted = sys.argv[1]
for cid, c in d["componentTypes"].items():
    if c["name"]["singular"] == wanted:
        for pid, p in sorted(c.get("properties", {}).items(), key=lambda kv: kv[1].get("displayOrder", 0)):
            lov = d["properties"].get(pid, {}).get("lov", {}).get("values") or []
            print(cid, f"{p.get('groupName')}.{p.get('propertyName')}", [v.get("name") for v in lov][:12])
PY
```

Replace `automation` with the failing component (`taskDefinition`, `workflow`, `activity`, `process`, `appItem`). Native plugin attributes (`backgroundExecution.runInBackground`, `timeout.timeoutType`) are in `apexlang.zip` under `apexlangmeta/native-plugins/shared-components/plugins/process/<plugin>/custom-attributes.apx`.

- [x] **Step 12: Commit and push in the user-requested integration changeset**

```bash
git add -A
git commit -m "feat: add project APEX skills and optional uc-apx setup"
git push origin HEAD:main
```

The user explicitly authorized this integration commit and direct push to
`main`. Delete the local task branch only after the push succeeds.

---

### Task 4: Run the probe and record findings (database write; ask the user first)

**Files:**
- Create: `.agents/skills/apex-background/findings-apex-26.1.md`

**Interfaces:**
- Consumes: Task 3 runner and app.
- Produces: the findings matrix that Task 5 cites. It has one column per context in the Task 3 table, plus `SQLCL_NO_SESSION` and `SQLCL_APEX_SESSION`.

- [x] **Step 1: Start the run**

Run: `.agents/skills/apex-background/probe/run.sh start docker-demo DEMO DEMO`
Expected: `APEX_BG_PROBE_STARTED`, a standalone task ID, a workflow ID, and the page URL.

- [x] **Step 2: Submit the page in the built-in browser**

Open the printed URL (`http://localhost:8181/ords/r/demo/apex-bg-probe/home`) with the browser pane, click **Run probe**, and confirm that the page reloads without an error. The app uses No Authentication, so no credentials are entered.

- [x] **Step 3: Finish and render**

Run:

```bash
.agents/skills/apex-background/probe/run.sh finish docker-demo DEMO DEMO
APEX_VERSION=26.1.4 DATABASE_NAME=FREEPDB1 .agents/skills/apex-background/probe/run.sh report docker-demo DEMO DEMO \
  > .agents/skills/apex-background/findings-apex-26.1.md
```

Expected: the findings table has 13 context columns. If a context column is missing, or `## Faults` lists a bind activity, remove only the named failing bind from that component in `probe/app`, then rerun `install`, `start`, the browser step, `finish`, and `report`. Record the removed bind and the fault message in the findings file under a `## Unsupported binds` heading.

- [x] **Step 4: Review the findings**

Read the whole matrix and check that each cell is plausible (for example, `SQLCL_NO_SESSION` has `<null>` for `V(APP_SESSION)`).

The matrix was checked against the required markers, observed binds, and
context-specific session values. The user explicitly authorized including the
findings in the final integration commit; no separate findings-only commit is
needed.

**Run result:** APEX 26.1.4 / FREEPDB1 returned 1,474 observation rows across
13 contexts. All required completion markers were present, the finisher
completed the run, and its final runtime-fault count was zero. `faults.csv`
for the rendered evidence contains no faults; local `.previous-*` files retain
the earlier failed-attempt outputs for diagnosis. The matrix captures
`APEX$TASK_TEXT` in Add Comment only; Request Information and Submit
Information remain unverified.

---

### Task 5: Write the skill from the findings

**Files:**
- Create: `.agents/skills/apex-background/SKILL.md`, `.claude/skills/apex-background/SKILL.md`
- Modify: `tests/test_apex_background_skill.py`, `AGENTS.md` (one line in "Rules for coding agents")

**Interfaces:**
- Consumes: `findings-apex-26.1.md` from Task 4.

- [x] **Step 1: Add the failing skill contract tests**

Append to `tests/test_apex_background_skill.py`:

```python
class SkillContractTests(unittest.TestCase):
    def test_skill_frontmatter_names_the_directory(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: apex-background\ndescription: "))
        self.assertIn("\n---\n", text[4:])

    def test_claude_copy_is_byte_identical(self) -> None:
        self.assertEqual(
            (ROOT / ".claude" / "skills" / "apex-background" / "SKILL.md").read_bytes(),
            (SKILL / "SKILL.md").read_bytes(),
        )

    def test_skill_cites_findings_for_every_context(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        findings = (SKILL / "findings-apex-26.1.md").read_text(encoding="utf-8")
        header = next(line for line in findings.splitlines() if line.startswith("| Probe |"))
        contexts = [c.strip() for c in header.strip("|").split("|")[1:]]
        self.assertGreaterEqual(len(contexts), 13)
        for context in contexts:
            with self.subTest(context=context):
                self.assertIn(context, skill)
        self.assertIn("findings-apex-26.1.md", skill)
        self.assertIn("APEX 26.1.4", skill)

    def test_agents_points_at_the_skill(self) -> None:
        self.assertIn(".agents/skills/apex-background/SKILL.md", (ROOT / "AGENTS.md").read_text(encoding="utf-8"))
```

Run: `python3 -m unittest tests.test_apex_background_skill -v`
Expected: the four new tests FAIL.

- [x] **Step 2: Write `SKILL.md` from the matrix**

Create `.agents/skills/apex-background/SKILL.md` with exactly this structure. Fill every table cell from `findings-apex-26.1.md`, citing the probe name. A cell with no observation says `not verified on 26.1`.

````markdown
---
name: apex-background
description: Use when writing or debugging Oracle APEX PL/SQL that runs outside a page request (automations, workflow activities, human task actions, and background execution chains) to know which APEX session, session state, bind variables, and substitution strings exist there. Verified on APEX 26.1.4.
---

# APEX background execution context

Code in APEX automations, workflow activities, task actions, and background
execution chains does not run inside the browser request that triggered it.
Before relying on `:APP_USER`, `v('P1_ITEM')`, `&APP_TITLE.`, or
`apex_application.g_*`, check this table. Evidence:
`.agents/skills/apex-background/findings-apex-26.1.md` (APEX 26.1.4,
FREEPDB1). Re-verify on another version with
`.agents/skills/apex-background/probe/run.sh`.

## Session by context

| Context | APEX session (`V(APP_SESSION)`) | `APP_USER` | App/page items | `&SUBST.` via `do_substitutions` | DB job (`USERENV BG_JOB_ID`) |
| --- | --- | --- | --- | --- | --- |
| SQLCL_NO_SESSION | … | … | … | … | … |
| SQLCL_APEX_SESSION | … | … | … | … | … |
| AUTOMATION_ON_DEMAND | … | … | … | … | … |
| AUTOMATION_ON_DEMAND_BACKGROUND | … | … | … | … | … |
| AUTOMATION_SCHEDULED | … | … | … | … | … |
| WORKFLOW_START | … | … | … | … | … |
| WORKFLOW_AFTER_WAIT | … | … | … | … | … |
| WORKFLOW_AFTER_TASK | … | … | … | … | … |
| TASK_ACTION_CREATE | … | … | … | … | … |
| TASK_ACTION_COMPLETE | … | … | … | … | … |
| TASK_ACTION_COMMENT | … | … | … | … | … |
| PAGE_PROCESS_FOREGROUND | … | … | … | … | … |
| EXECUTION_CHAIN_BACKGROUND | … | … | … | … | … |

## Bind variables by context

One row per `BIND :<name>` probe with a non-null value in the findings, and
the contexts where it had a value.

## Official substitution strings

Use the [Oracle APEX 26.1 built-in substitution reference](https://docs.oracle.com/en/database/oracle/apex/26.1/htmdb/using-available-built-in-substitution-strings.html) as the full general catalog. This probe records the documented `&NAME.` forms in every context and keeps template-only values, page/row values, and direct-PL/SQL forms identified by their official scope. `APP_NAME` is not an official 26.1 built-in; use `APP_TITLE`.

Workflow probes cover all six official names: `APEX$WORKFLOW_ACTIVITY_ID`, `APEX$WORKFLOW_CREATED_ON`, `APEX$WORKFLOW_DETAIL_PK`, `APEX$WORKFLOW_ID`, `APEX$WORKFLOW_INITIATOR`, and `APEX$WORKFLOW_STATE`.

Task probes cover all thirteen official names: `APEX$TASK_CREATED_ON`, `APEX$TASK_DUE_ON`, `APEX$TASK_ID`, `APEX$TASK_INITIATOR`, `APEX$TASK_MAX_RENEWAL_COUNT`, `APEX$TASK_OUTCOME`, `APEX$TASK_OWNER`, `APEX$TASK_PK`, `APEX$TASK_PREVIOUS_ID`, `APEX$TASK_RENEWAL_COUNT`, `APEX$TASK_STATE`, `APEX$TASK_SUBJECT`, and `APEX$TASK_TEXT`. The last value is observed in Add Comment; Request Information and Submit Information remain explicitly unverified.

## Rules

Write one rule per observed difference, each with its evidence. For example:
"Pass data to a workflow as parameters or `p_detail_pk`, not page items:
`V(P1_PROBE_ITEM)` is `<null>` in WORKFLOW_AFTER_WAIT." Include how to set an
APEX session when one is missing (`apex_session.create_session`) only if a
context showed no session.

## Hypothesis checked

"Background actions have no APEX session except task actions": state
whether the findings confirmed it, per context.
````

(The `…` cells in this skeleton are the ones Step 2 fills from the findings; the finished file must contain no `…`.)

- [x] **Step 3: Mirror for Claude Code and point AGENTS.md at it**

```bash
mkdir -p .claude/skills/apex-background
cp .agents/skills/apex-background/SKILL.md .claude/skills/apex-background/SKILL.md
```

In `AGENTS.md` under `## Rules for coding agents`, add:

```markdown
- Before writing PL/SQL for APEX automations, workflow activities, task
  actions, or background execution chains, read
  `.agents/skills/apex-background/SKILL.md`.
```

- [x] **Step 4: Verify**

Run: `grep -n '…' .agents/skills/apex-background/SKILL.md; python3 -m unittest discover -s tests`
Expected: no `…` lines, and `OK`.

Verification is complete. The user explicitly authorized including the skill
and its verification changes in the final integration commit.

---

### Task 6: Remove the probe from the database (ask the user first)

- [x] **Step 1: Uninstall**

Run: `.agents/skills/apex-background/probe/run.sh uninstall docker-demo DEMO DEMO`
Expected: `APEX_BG_PROBE_UNINSTALLED`.

- [x] **Step 2: Confirm nothing is left**

Run as DEMO:

```sql
SELECT COUNT(*) FROM user_objects WHERE object_name LIKE 'APEX_BG_PROBE%';
SELECT COUNT(*) FROM apex_applications WHERE application_id = 9901;
```

Expected: `0` and `0`. Report both counts to the user.

Observed after uninstall: `USER_OBJECTS` count `0`; `APEX_APPLICATIONS` count
for application 9901 `0`.

---

## Self-Review Notes

- Spec coverage: substitution strings (the `DO_SUBSTITUTIONS` probes and `PROBE_SUBST`), background processes (execution chain), automation code (three automations), workflow code activities (start, after Wait, after task), task actions (create and complete events), task actions inside workflows (`probe-task` uses `bg-probe-task`), and the session hypothesis (the SKILL.md "Hypothesis checked" section).
- Known uncertainty: some APEXlang names (`executionChain: @...` on child processes, `staticValue` participant values, Wait `timeout` attributes) come from grammar metadata, not from an exported example. Task 3 Step 11 is the verification gate. The fault-view column names are checked in Task 3 Step 8.
- The `template-manifest.json` from the template upgrade plan already treats `.agents/skills/**` and `.claude/skills/**` as template-owned, so downstream projects receive this skill.
