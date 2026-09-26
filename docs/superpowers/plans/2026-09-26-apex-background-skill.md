# APEX Background Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `apex-background` agent skill that tells developers and coding agents what APEX session, session state, bind variables, and substitution strings are available to PL/SQL that runs outside an interactive page request: automations, workflow activities, human task actions (standalone and inside workflows), and background execution chains. Every claim is backed by a probe run on APEX 26.1.

**Architecture:** Discovery first, documentation second. A self-contained probe ships with the skill under `.agents/skills/apex-background/probe/`:

- database objects (a run table, a log table, and a capture package);
- an APEXlang probe application (app 9901), built from SQLcl's bundled starter app plus one component per background context;
- a Bash runner.

Each context calls the capture package, which evaluates a fixed list of session probes in its own exception handler. Separate components record bind variables, so an unsupported bind cannot hide the session probes. A tested Python renderer turns the logged rows into `findings-apex-26.1.md`, and `SKILL.md` is written only from that matrix. The canonical skill lives in `.agents/skills/apex-background/`; a byte-identical `SKILL.md` in `.claude/skills/apex-background/` lets Claude Code discover it.

**Tech Stack:** Oracle APEX 26.1.4, APEXlang, SQLcl 26.2, PL/SQL, Bash, Python 3.10+ standard library, `unittest`, the built-in browser pane (for one page submit).

**Spec:** This plan is the spec. User request (2026-09-26): "I need to make some extra skills for Oracle APEX to be added to this repo. Let's start with [call this the APEX background skill, because background actions do not have APEX sessions, I think, except for tasks]: substitution strings discovery in the background processes, automation and workflow code activities, and APEX task actions and task actions in workflow activities."

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
| `.agents/skills/apex-background/probe/uninstall.sql` | Removes probe objects |
| `.agents/skills/apex-background/probe/contexts.sql` | Starts a run and triggers SQLcl-driven contexts |
| `.agents/skills/apex-background/probe/finish.sql` | Waits, approves tasks, disables the schedule, spools CSV |
| `.agents/skills/apex-background/probe/app/` | APEXlang probe application source |
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
  - Output: a header with the versions, a Markdown table with one row per probe and one column per context (contexts in first-seen order), then a `## Faults` list. A cell is the value, `<null>`, `error: <message>`, or `-` when the context did not record that probe.

- [ ] **Step 1: Write the failing renderer tests**

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
            '"WORKFLOW_START","V(APP_SESSION)","<null>",\n'
            '"SQLCL_APEX_SESSION","BIND :APEX$TASK_ID","",\n'
            '"WORKFLOW_START","DO_SUBSTITUTIONS(&APP_NAME.)",,"ORA-06550: boom"\n',
            '"SOURCE","NAME","DETAIL"\n',
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("APEX 26.1.4", result.stdout)
        self.assertIn("| Probe | SQLCL_APEX_SESSION | WORKFLOW_START |", lines)
        self.assertIn("| V(APP_SESSION) | 1234 | <null> |", lines)
        self.assertIn("| BIND :APEX$TASK_ID | <null> | - |", lines)
        self.assertIn("| DO_SUBSTITUTIONS(&APP_NAME.) | - | error: ORA-06550: boom |", lines)
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

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python3 -m unittest tests.test_apex_background_skill -v`
Expected: FAIL, `can't open file ... render_findings.py`.

- [ ] **Step 3: Implement the renderer**

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

- [ ] **Step 4: Run the tests, lint, and commit**

Run: `python3 -m unittest tests.test_apex_background_skill -v && .venv/bin/ruff check .agents tests`
Expected: PASS and `All checks passed!`.

```bash
git add .agents/skills/apex-background/probe/render_findings.py tests/test_apex_background_skill.py
git commit -m "feat: add APEX background probe findings renderer"
```

---

### Task 2: Probe database objects

**Files:**
- Create: `.agents/skills/apex-background/probe/install.sql`, `.agents/skills/apex-background/probe/uninstall.sql`
- Modify: `tests/test_apex_background_skill.py`

**Interfaces:**
- Produces, in the probe app's parsing schema:
  - tables `APEX_BG_PROBE_RUN(run_id, label, started_at)` and `APEX_BG_PROBE_LOG(log_id, run_id, context_name, probe_name, probe_value, probe_error, captured_at)`;
  - package `APEX_BG_PROBE` with `start_run(p_label VARCHAR2)`, `current_run RETURN NUMBER`, `capture(p_context VARCHAR2)`, and `capture_bind(p_context VARCHAR2, p_name VARCHAR2, p_value VARCHAR2)`.
- Every write is an autonomous transaction, so rows survive a failing component. `capture` records these probe names, which later tasks and the skill rely on:
  - session state: `V(APP_ID)`, `V(APP_PAGE_ID)`, `V(APP_SESSION)`, `V(APP_USER)`, `V(APP_ALIAS)`, `V(WORKSPACE_ID)`, `V(PROBE_APP_ITEM)`, `V(P1_PROBE_ITEM)`;
  - globals: `APEX_APPLICATION.G_FLOW_ID`, `APEX_APPLICATION.G_FLOW_STEP_ID`, `APEX_APPLICATION.G_INSTANCE`, `APEX_APPLICATION.G_USER`;
  - session context: `SYS_CONTEXT(APEX$SESSION,APP_SESSION)`, `SYS_CONTEXT(APEX$SESSION,APP_USER)`, `SYS_CONTEXT(APEX$SESSION,WORKSPACE_ID)`;
  - substitutions: `DO_SUBSTITUTIONS(&APP_ID.)`, `DO_SUBSTITUTIONS(&APP_USER.)`, `DO_SUBSTITUTIONS(&APP_NAME.)`, `DO_SUBSTITUTIONS(&PROBE_SUBST.)`, `DO_SUBSTITUTIONS(&PROBE_APP_ITEM.)`, `DO_SUBSTITUTIONS(&P1_PROBE_ITEM.)`;
  - database session: `USERENV SESSION_USER`, `USERENV CURRENT_SCHEMA`, `USERENV MODULE`, `USERENV ACTION`, `USERENV CLIENT_IDENTIFIER`, `USERENV CLIENT_INFO`, `USERENV BG_JOB_ID`, `USERENV SID`, and `SESSIONTIMEZONE`.
- `capture_bind` records `BIND :<name>`.

- [ ] **Step 1: Add a failing layout test**

Append to `tests/test_apex_background_skill.py`:

```python
class ProbeLayoutTests(unittest.TestCase):
    def test_install_defines_every_probe_the_skill_relies_on(self) -> None:
        install = (SKILL / "probe" / "install.sql").read_text(encoding="utf-8")
        for probe in (
            "V(APP_SESSION)", "V(APP_USER)", "V(PROBE_APP_ITEM)", "V(P1_PROBE_ITEM)",
            "APEX_APPLICATION.G_INSTANCE", "SYS_CONTEXT(APEX$SESSION,APP_SESSION)",
            "DO_SUBSTITUTIONS(&APP_NAME.)", "DO_SUBSTITUTIONS(&PROBE_SUBST.)",
            "USERENV BG_JOB_ID", "USERENV MODULE",
        ):
            with self.subTest(probe=probe):
                self.assertIn(f"'{probe}'", install)
        self.assertIn("PRAGMA AUTONOMOUS_TRANSACTION", install)
        self.assertIn("SET DEFINE OFF", install)

    def test_uninstall_removes_every_installed_object(self) -> None:
        uninstall = (SKILL / "probe" / "uninstall.sql").read_text(encoding="utf-8")
        for statement in ("DROP PACKAGE apex_bg_probe", "DROP TABLE apex_bg_probe_log", "DROP TABLE apex_bg_probe_run"):
            with self.subTest(statement=statement):
                self.assertIn(statement, uninstall)
```

Run: `python3 -m unittest tests.test_apex_background_skill -v`
Expected: the two new tests ERROR with `FileNotFoundError`.

- [ ] **Step 2: Write `install.sql`**

```sql
-- APEX background probe objects. Run as the probe application's parsing schema.
-- Writes only APEX_BG_PROBE* objects. Re-runnable.
SET DEFINE OFF
WHENEVER SQLERROR EXIT FAILURE ROLLBACK

CREATE TABLE IF NOT EXISTS apex_bg_probe_run (
  run_id     NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  label      VARCHAR2(200) NOT NULL,
  started_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS apex_bg_probe_log (
  log_id       NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id       NUMBER NOT NULL REFERENCES apex_bg_probe_run,
  context_name VARCHAR2(60) NOT NULL,
  probe_name   VARCHAR2(128) NOT NULL,
  probe_value  VARCHAR2(4000),
  probe_error  VARCHAR2(4000),
  captured_at  TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE OR REPLACE PACKAGE apex_bg_probe AUTHID DEFINER AS
  PROCEDURE start_run(p_label IN VARCHAR2);
  FUNCTION current_run RETURN NUMBER;
  PROCEDURE capture(p_context IN VARCHAR2);
  PROCEDURE capture_bind(p_context IN VARCHAR2, p_name IN VARCHAR2, p_value IN VARCHAR2);
END apex_bg_probe;
/

CREATE OR REPLACE PACKAGE BODY apex_bg_probe AS
  PROCEDURE log_value(p_context VARCHAR2, p_name VARCHAR2, p_value VARCHAR2, p_error VARCHAR2 DEFAULT NULL) IS
    PRAGMA AUTONOMOUS_TRANSACTION;
  BEGIN
    INSERT INTO apex_bg_probe_log (run_id, context_name, probe_name, probe_value, probe_error)
    VALUES (current_run, p_context, p_name, SUBSTR(p_value, 1, 4000), SUBSTR(p_error, 1, 4000));
    COMMIT;
  END log_value;

  PROCEDURE start_run(p_label IN VARCHAR2) IS
    PRAGMA AUTONOMOUS_TRANSACTION;
  BEGIN
    INSERT INTO apex_bg_probe_run (label) VALUES (p_label);
    COMMIT;
  END start_run;

  FUNCTION current_run RETURN NUMBER IS
    l_run NUMBER;
  BEGIN
    SELECT MAX(run_id) INTO l_run FROM apex_bg_probe_run;
    RETURN l_run;
  END current_run;

  -- Each probe is evaluated separately so one unavailable API cannot hide the rest.
  PROCEDURE probe(p_context VARCHAR2, p_name VARCHAR2, p_expression VARCHAR2) IS
    l_value VARCHAR2(4000);
  BEGIN
    EXECUTE IMMEDIATE 'BEGIN :v := ' || p_expression || '; END;' USING OUT l_value;
    log_value(p_context, p_name, NVL(l_value, '<null>'));
  EXCEPTION
    WHEN OTHERS THEN
      log_value(p_context, p_name, NULL, SQLERRM);
  END probe;

  PROCEDURE capture(p_context IN VARCHAR2) IS
  BEGIN
    probe(p_context, 'V(APP_ID)', q'[v('APP_ID')]');
    probe(p_context, 'V(APP_PAGE_ID)', q'[v('APP_PAGE_ID')]');
    probe(p_context, 'V(APP_SESSION)', q'[v('APP_SESSION')]');
    probe(p_context, 'V(APP_USER)', q'[v('APP_USER')]');
    probe(p_context, 'V(APP_ALIAS)', q'[v('APP_ALIAS')]');
    probe(p_context, 'V(WORKSPACE_ID)', q'[v('WORKSPACE_ID')]');
    probe(p_context, 'V(PROBE_APP_ITEM)', q'[v('PROBE_APP_ITEM')]');
    probe(p_context, 'V(P1_PROBE_ITEM)', q'[v('P1_PROBE_ITEM')]');
    probe(p_context, 'APEX_APPLICATION.G_FLOW_ID', 'apex_application.g_flow_id');
    probe(p_context, 'APEX_APPLICATION.G_FLOW_STEP_ID', 'apex_application.g_flow_step_id');
    probe(p_context, 'APEX_APPLICATION.G_INSTANCE', 'apex_application.g_instance');
    probe(p_context, 'APEX_APPLICATION.G_USER', 'apex_application.g_user');
    probe(p_context, 'SYS_CONTEXT(APEX$SESSION,APP_SESSION)', q'[sys_context('APEX$SESSION', 'APP_SESSION')]');
    probe(p_context, 'SYS_CONTEXT(APEX$SESSION,APP_USER)', q'[sys_context('APEX$SESSION', 'APP_USER')]');
    probe(p_context, 'SYS_CONTEXT(APEX$SESSION,WORKSPACE_ID)', q'[sys_context('APEX$SESSION', 'WORKSPACE_ID')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&APP_ID.)', q'[apex_application.do_substitutions('&APP_ID.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&APP_USER.)', q'[apex_application.do_substitutions('&APP_USER.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&APP_NAME.)', q'[apex_application.do_substitutions('&APP_NAME.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&PROBE_SUBST.)', q'[apex_application.do_substitutions('&PROBE_SUBST.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&PROBE_APP_ITEM.)', q'[apex_application.do_substitutions('&PROBE_APP_ITEM.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&P1_PROBE_ITEM.)', q'[apex_application.do_substitutions('&P1_PROBE_ITEM.')]');
    probe(p_context, 'USERENV SESSION_USER', q'[sys_context('USERENV', 'SESSION_USER')]');
    probe(p_context, 'USERENV CURRENT_SCHEMA', q'[sys_context('USERENV', 'CURRENT_SCHEMA')]');
    probe(p_context, 'USERENV MODULE', q'[sys_context('USERENV', 'MODULE')]');
    probe(p_context, 'USERENV ACTION', q'[sys_context('USERENV', 'ACTION')]');
    probe(p_context, 'USERENV CLIENT_IDENTIFIER', q'[sys_context('USERENV', 'CLIENT_IDENTIFIER')]');
    probe(p_context, 'USERENV CLIENT_INFO', q'[sys_context('USERENV', 'CLIENT_INFO')]');
    probe(p_context, 'USERENV BG_JOB_ID', q'[sys_context('USERENV', 'BG_JOB_ID')]');
    probe(p_context, 'USERENV SID', q'[sys_context('USERENV', 'SID')]');
    probe(p_context, 'SESSIONTIMEZONE', 'sessiontimezone');
  END capture;

  PROCEDURE capture_bind(p_context IN VARCHAR2, p_name IN VARCHAR2, p_value IN VARCHAR2) IS
  BEGIN
    log_value(p_context, 'BIND :' || p_name, NVL(p_value, '<null>'));
  END capture_bind;
END apex_bg_probe;
/

PROMPT APEX_BG_PROBE_INSTALLED
EXIT SUCCESS COMMIT
```

- [ ] **Step 3: Write `uninstall.sql`**

```sql
-- Remove APEX background probe objects. Application 9901 is removed by run.sh uninstall.
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
DROP PACKAGE apex_bg_probe;
DROP TABLE apex_bg_probe_log PURGE;
DROP TABLE apex_bg_probe_run PURGE;
PROMPT APEX_BG_PROBE_UNINSTALLED
EXIT SUCCESS COMMIT
```

- [ ] **Step 4: Run the tests and commit**

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
    | workflow `bg-probe-workflow`, at start | `WORKFLOW_START` |
    | workflow `bg-probe-workflow`, after a one-minute Wait | `WORKFLOW_AFTER_WAIT` |
    | workflow `bg-probe-workflow`, after its human task completes | `WORKFLOW_AFTER_TASK` |
    | page 1 process, foreground | `PAGE_PROCESS_FOREGROUND` |
    | page 1 execution chain, background | `EXECUTION_CHAIN_BACKGROUND` |

  - The SQLcl-driven contexts are `SQLCL_NO_SESSION` and `SQLCL_APEX_SESSION`.
  - `run.sh <install|start|finish|report|uninstall> <sqlcl-connection> <workspace> <parsing-schema>`.

- [ ] **Step 1: Add a failing app layout test**

Append to `ProbeLayoutTests`:

```python
    def test_probe_app_defines_every_background_context(self) -> None:
        app = SKILL / "probe" / "app"
        source = "\n".join(p.read_text(encoding="utf-8") for p in app.rglob("*.apx"))
        for context in (
            "AUTOMATION_ON_DEMAND'", "AUTOMATION_ON_DEMAND_BACKGROUND'", "AUTOMATION_SCHEDULED'",
            "TASK_ACTION_CREATE'", "TASK_ACTION_COMPLETE'", "WORKFLOW_START'", "WORKFLOW_AFTER_WAIT'",
            "WORKFLOW_AFTER_TASK'", "PAGE_PROCESS_FOREGROUND'", "EXECUTION_CHAIN_BACKGROUND'",
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

- [ ] **Step 2: Copy the SQLcl starter app**

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

- [ ] **Step 3: Switch to No Authentication and add the substitution string**

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

Create `$PROBE/app/shared-components/app-items.apx`:

```text
appItem PROBE_APP_ITEM (
    security {
        sessionStateProtection: unrestricted
    }
)
```

- [ ] **Step 4: Add the three automations**

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

- [ ] **Step 5: Add the task definition**

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
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_ID', :APEX$TASK_ID);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_PK', :APEX$TASK_PK);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_OWNER', :APEX$TASK_OWNER);
                    apex_bg_probe.capture_bind('TASK_ACTION_CREATE', 'APEX$TASK_INITIATOR', :APEX$TASK_INITIATOR);
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
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_ID', :APEX$TASK_ID);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_PK', :APEX$TASK_PK);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_OUTCOME', :APEX$TASK_OUTCOME);
                    apex_bg_probe.capture_bind('TASK_ACTION_COMPLETE', 'APEX$TASK_OWNER', :APEX$TASK_OWNER);
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

    participant (
        value {
            type: staticValue
            staticValue: PROBE_USER
        }
    )
)
````

- [ ] **Step 6: Add the workflow**

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
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'APEX$WORKFLOW_ID', :APEX$WORKFLOW_ID);
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'APEX$WORKFLOW_DETAIL_PK', :APEX$WORKFLOW_DETAIL_PK);
                        apex_bg_probe.capture_bind('WORKFLOW_START', 'APEX$WORKFLOW_INITIATOR', :APEX$WORKFLOW_INITIATOR);
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
                plsqlCode: apex_bg_probe.capture('WORKFLOW_AFTER_WAIT');
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
                        apex_bg_probe.capture_bind('WORKFLOW_AFTER_TASK', 'APEX$WORKFLOW_ID', :APEX$WORKFLOW_ID);
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

- [ ] **Step 7: Replace page 1 with the background-chain page**

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

- [ ] **Step 8: Write the SQLcl context and finish scripts**

Create `.agents/skills/apex-background/probe/contexts.sql`:

```sql
-- Arguments: application id, run label. Starts a run and triggers the SQLcl-driven contexts.
SET DEFINE ON
SET SERVEROUTPUT ON
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
DEFINE app_id = '&1'
DEFINE run_label = '&2'

BEGIN
  apex_bg_probe.start_run('&&run_label');
  apex_bg_probe.capture('SQLCL_NO_SESSION');

  apex_session.create_session(p_app_id => &&app_id, p_page_id => 1, p_username => 'PROBE_USER');
  apex_util.set_session_state('PROBE_APP_ITEM', 'app-item-from-sqlcl');
  apex_util.set_session_state('P1_PROBE_ITEM', 'page-item-from-sqlcl');
  apex_bg_probe.capture('SQLCL_APEX_SESSION');

  apex_automation.execute(p_application_id => &&app_id, p_static_id => 'bg-probe-on-demand', p_run_in_background => FALSE);
  apex_automation.execute(p_application_id => &&app_id, p_static_id => 'bg-probe-on-demand-bg', p_run_in_background => TRUE);
  apex_automation.enable(p_application_id => &&app_id, p_static_id => 'bg-probe-scheduled');

  DBMS_OUTPUT.PUT_LINE('standalone task ' || apex_human_task.create_task(
    p_application_id => &&app_id, p_task_def_static_id => 'bg-probe-task', p_subject => 'BG probe standalone',
    p_initiator => 'PROBE_USER', p_initiator_can_complete => TRUE, p_detail_pk => 'standalone'));
  DBMS_OUTPUT.PUT_LINE('workflow ' || apex_workflow.start_workflow(
    p_application_id => &&app_id, p_static_id => 'bg-probe-workflow', p_initiator => 'PROBE_USER',
    p_detail_pk => 'workflow'));
  COMMIT;
END;
/
PROMPT APEX_BG_PROBE_STARTED
EXIT SUCCESS COMMIT
```

Create `.agents/skills/apex-background/probe/finish.sql`:

```sql
-- Arguments: application id. Waits for the Wait activity, approves open probe
-- tasks, disables the schedule, and spools log.csv and faults.csv.
SET DEFINE ON
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
DEFINE app_id = '&1'

DECLARE
  PROCEDURE wait_for(p_context VARCHAR2, p_seconds PLS_INTEGER) IS
    l_count PLS_INTEGER;
  BEGIN
    FOR i IN 1 .. p_seconds / 5 LOOP
      SELECT COUNT(*) INTO l_count FROM apex_bg_probe_log
       WHERE run_id = apex_bg_probe.current_run AND context_name = p_context;
      EXIT WHEN l_count > 0;
      DBMS_SESSION.SLEEP(5);
    END LOOP;
  END;
BEGIN
  wait_for('WORKFLOW_AFTER_WAIT', 240);
  wait_for('AUTOMATION_SCHEDULED', 120);
  apex_session.create_session(p_app_id => &&app_id, p_page_id => 1, p_username => 'PROBE_USER');
  FOR t IN (SELECT task_id FROM apex_tasks
             WHERE application_id = &&app_id AND state_code IN ('UNASSIGNED', 'ASSIGNED')) LOOP
    apex_human_task.approve_task(p_task_id => t.task_id, p_autoclaim => TRUE);
  END LOOP;
  COMMIT;
  wait_for('WORKFLOW_AFTER_TASK', 120);
  apex_automation.disable(p_application_id => &&app_id, p_static_id => 'bg-probe-scheduled');
  COMMIT;
END;
/

SET SQLFORMAT CSV
SET FEEDBACK OFF
SPOOL log.csv
SELECT context_name, probe_name, probe_value, probe_error
  FROM apex_bg_probe_log
 WHERE run_id = apex_bg_probe.current_run
 ORDER BY log_id;
SPOOL OFF
SPOOL faults.csv
SELECT 'workflow activity' AS source, activity_static_id AS name, state_code || ': ' || error_message AS detail
  FROM apex_workflow_activities
 WHERE application_id = &&app_id AND state_code IN ('FAULTED', 'ERROR')
UNION ALL
SELECT 'automation', automation_static_id, message
  FROM apex_automation_msg_log
 WHERE application_id = &&app_id AND message_type = 'ERROR'
UNION ALL
SELECT 'task', TO_CHAR(task_id), state_code
  FROM apex_tasks
 WHERE application_id = &&app_id AND state_code NOT IN ('COMPLETED');
SPOOL OFF
EXIT SUCCESS COMMIT
```

The column names in the three fault views (`activity_static_id`, `error_message`, `automation_static_id`, `message`, `message_type`) must be checked before the first run. Run `DESC apex_workflow_activities`, `DESC apex_automation_msg_log`, and `DESC apex_tasks` as the probe schema and adjust the fault query to the actual names. Do not skip the faults section.

- [ ] **Step 9: Write the runner**

Create `.agents/skills/apex-background/probe/run.sh` and `chmod +x` it:

```bash
#!/usr/bin/env bash
# APEX background probe. Every phase except report writes to the target
# database: probe objects and application 9901. Use only a DEV database.
set -euo pipefail

usage() {
  printf 'usage: run.sh <install|start|finish|report|uninstall> <sqlcl-connection> <workspace> <parsing-schema>\n' >&2
  exit 2
}
[ "$#" -eq 4 ] || usage
phase="$1" connection="$2" workspace="$3" schema="$4"
app_id=9901
probe_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
state_dir="$probe_dir/.run"
mkdir -p "$state_dir"
: > "$state_dir/.stdin"

sqlcl() { sql -S -noupdates -name "$connection" "$@" < "$state_dir/.stdin"; }
require_line() { grep -Eq "^[[:space:]]*$1[[:space:]]*$" "$2" || { cat "$2" >&2; printf 'probe error: missing %s\n' "$1" >&2; exit 1; }; }

case "$phase" in
  install)
    (cd "$probe_dir" && sqlcl @install.sql) | tee "$state_dir/install.log"
    require_line APEX_BG_PROBE_INSTALLED "$state_dir/install.log"
    rm -rf "$state_dir/app" && cp -R "$probe_dir/app" "$state_dir/app"
    mkdir -p "$state_dir/app/deployments"
    printf '{"workspace":{"name":"%s"},"app":{"id":%s,"databaseSession":{"parsingSchema":"%s"}}}\n' \
      "$workspace" "$app_id" "$schema" > "$state_dir/app/deployments/probe.json"
    printf 'apex import -input . -deployment deployments/probe.json\nexit\n' > "$state_dir/import.sql"
    (cd "$state_dir/app" && sqlcl "@$state_dir/import.sql") | tee "$state_dir/import.log"
    require_line 'Import successful\.' "$state_dir/import.log"
    ;;
  start)
    (cd "$probe_dir" && sqlcl @contexts.sql "$app_id" "apex-bg-probe $(date -u +%Y-%m-%dT%H:%M:%SZ)") | tee "$state_dir/start.log"
    require_line APEX_BG_PROBE_STARTED "$state_dir/start.log"
    printf 'Now open %s and click "Run probe", then run the finish phase.\n' \
      "http://localhost:8181/ords/r/$(printf '%s' "$workspace" | tr '[:upper:]' '[:lower:]')/apex-bg-probe/home"
    ;;
  finish)
    (cd "$state_dir" && sqlcl "@$probe_dir/finish.sql" "$app_id")
    test -s "$state_dir/log.csv" || { printf 'probe error: log.csv was not written\n' >&2; exit 1; }
    ;;
  report)
    python3 "$probe_dir/render_findings.py" "$state_dir/log.csv" "$state_dir/faults.csv" \
      --apex "${APEX_VERSION:?set APEX_VERSION}" --database "${DATABASE_NAME:?set DATABASE_NAME}"
    ;;
  uninstall)
    printf "begin apex_util.set_workspace('%s'); apex_application_install.remove_application(%s); commit; end;\n/\nexit\n" \
      "$workspace" "$app_id" > "$state_dir/remove.sql"
    sqlcl "@$state_dir/remove.sql"
    (cd "$probe_dir" && sqlcl @uninstall.sql)
    rm -rf "$state_dir"
    ;;
  *) usage ;;
esac
```

Add `.agents/skills/apex-background/probe/.run/` to `.gitignore`.

- [ ] **Step 10: Run the layout tests and shellcheck**

Run: `python3 -m unittest tests.test_apex_background_skill -v && shellcheck -S warning .agents/skills/apex-background/probe/run.sh`
Expected: PASS, no warnings.

- [ ] **Step 11: Install on docker-demo (database write; ask the user first)**

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

- [ ] **Step 12: Commit**

```bash
git add .agents/skills/apex-background/probe .gitignore tests/test_apex_background_skill.py
git commit -m "feat: add APEX background probe application and runner"
```

---

### Task 4: Run the probe and record findings (database write; ask the user first)

**Files:**
- Create: `.agents/skills/apex-background/findings-apex-26.1.md`

**Interfaces:**
- Consumes: Task 3 runner and app.
- Produces: the findings matrix that Task 5 cites. It has one column per context in the Task 3 table, plus `SQLCL_NO_SESSION` and `SQLCL_APEX_SESSION`.

- [ ] **Step 1: Start the run**

Run: `.agents/skills/apex-background/probe/run.sh start docker-demo DEMO DEMO`
Expected: `APEX_BG_PROBE_STARTED`, a standalone task ID, a workflow ID, and the page URL.

- [ ] **Step 2: Submit the page in the built-in browser**

Open the printed URL (`http://localhost:8181/ords/r/demo/apex-bg-probe/home`) with the browser pane, click **Run probe**, and confirm that the page reloads without an error. The app uses No Authentication, so no credentials are entered.

- [ ] **Step 3: Finish and render**

Run:

```bash
.agents/skills/apex-background/probe/run.sh finish docker-demo DEMO DEMO
APEX_VERSION=26.1.4 DATABASE_NAME=FREEPDB1 .agents/skills/apex-background/probe/run.sh report docker-demo DEMO DEMO \
  > .agents/skills/apex-background/findings-apex-26.1.md
```

Expected: the findings table has 12 context columns. If a context column is missing, or `## Faults` lists a bind activity, remove only the named failing bind from that component in `probe/app`, then rerun `install`, `start`, the browser step, `finish`, and `report`. Record the removed bind and the fault message in the findings file under a `## Unsupported binds` heading.

- [ ] **Step 4: Review and commit**

Read the whole matrix and check that each cell is plausible (for example, `SQLCL_NO_SESSION` has `<null>` for `V(APP_SESSION)`).

```bash
git add .agents/skills/apex-background/findings-apex-26.1.md
git commit -m "docs: record APEX 26.1 background execution probe findings"
```

---

### Task 5: Write the skill from the findings

**Files:**
- Create: `.agents/skills/apex-background/SKILL.md`, `.claude/skills/apex-background/SKILL.md`
- Modify: `tests/test_apex_background_skill.py`, `AGENTS.md` (one line in "Rules for coding agents")

**Interfaces:**
- Consumes: `findings-apex-26.1.md` from Task 4.

- [ ] **Step 1: Add the failing skill contract tests**

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
        self.assertGreaterEqual(len(contexts), 12)
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

- [ ] **Step 2: Write `SKILL.md` from the matrix**

Create `.agents/skills/apex-background/SKILL.md` with exactly this structure. Fill every table cell from `findings-apex-26.1.md`, citing the probe name. A cell with no observation says `not verified on 26.1`.

````markdown
---
name: apex-background
description: Use when writing or debugging Oracle APEX PL/SQL that runs outside a page request (automations, workflow activities, human task actions, and background execution chains) to know which APEX session, session state, bind variables, and substitution strings exist there. Verified on APEX 26.1.4.
---

# APEX background execution context

Code in APEX automations, workflow activities, task actions, and background
execution chains does not run inside the browser request that triggered it.
Before relying on `:APP_USER`, `v('P1_ITEM')`, `&APP_NAME.`, or
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
| PAGE_PROCESS_FOREGROUND | … | … | … | … | … |
| EXECUTION_CHAIN_BACKGROUND | … | … | … | … | … |

## Bind variables by context

One row per `BIND :<name>` probe with a non-null value in the findings, and
the contexts where it had a value.

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

- [ ] **Step 3: Mirror for Claude Code and point AGENTS.md at it**

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

- [ ] **Step 4: Verify and commit**

Run: `grep -n '…' .agents/skills/apex-background/SKILL.md; python3 -m unittest discover -s tests`
Expected: no `…` lines, and `OK`.

```bash
git add .agents/skills/apex-background/SKILL.md .claude/skills/apex-background/SKILL.md AGENTS.md tests/test_apex_background_skill.py
git commit -m "feat: add apex-background skill backed by APEX 26.1 probe findings"
```

---

### Task 6: Remove the probe from the database (ask the user first)

- [ ] **Step 1: Uninstall**

Run: `.agents/skills/apex-background/probe/run.sh uninstall docker-demo DEMO DEMO`
Expected: `APEX_BG_PROBE_UNINSTALLED`.

- [ ] **Step 2: Confirm nothing is left**

Run as DEMO:

```sql
SELECT COUNT(*) FROM user_objects WHERE object_name LIKE 'APEX_BG_PROBE%';
SELECT COUNT(*) FROM apex_applications WHERE application_id = 9901;
```

Expected: `0` and `0`. Report both counts to the user.

---

## Self-Review Notes

- Spec coverage: substitution strings (the `DO_SUBSTITUTIONS` probes and `PROBE_SUBST`), background processes (execution chain), automation code (three automations), workflow code activities (start, after Wait, after task), task actions (create and complete events), task actions inside workflows (`probe-task` uses `bg-probe-task`), and the session hypothesis (the SKILL.md "Hypothesis checked" section).
- Known uncertainty: some APEXlang names (`executionChain: @...` on child processes, `staticValue` participant values, Wait `timeout` attributes) come from grammar metadata, not from an exported example. Task 3 Step 11 is the verification gate. The fault-view column names are checked in Task 3 Step 8.
- The `template-manifest.json` from the template upgrade plan already treats `.agents/skills/**` and `.claude/skills/**` as template-owned, so downstream projects receive this skill.
