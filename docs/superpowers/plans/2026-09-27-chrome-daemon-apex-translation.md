# Chrome Daemon and APEX Translation Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port a project-neutral persistent Chrome DevTools daemon into this repository, update its APEX browser guidance and text-message safeguards, and preserve the verified app 150 results.

**Architecture:** Keep the daemon and allowlisted client as stdlib-only Python modules under `tools/`, with private per-user UNIX socket transport and no client autostart. Put portable operating instructions in a focused browser skill and project rule; record translation protections and dated app 150 evidence in the existing translation skill.

**Tech Stack:** Python 3.10+, `unittest`, Chrome DevTools MCP, Markdown, SQLcl read-only metadata checks.

**Spec:** `docs/superpowers/specs/2026-09-27-chrome-daemon-apex-translation-design.md`

## Global Constraints

- Use a normal Git branch; do not create a worktree.
- Run browser calls only through `tools/chrome_devtools_client.py` and the persistent daemon. The client must not silently start a daemon.
- Keep the socket in the existing per-user runtime location so projects under this account can share the one consented Chrome session. Use generic environment variable names and a generic `/tmp` fallback.
- Do not store credentials, session URLs, or session identifiers in the repository or final report.
- The app 150 check is read-only. No app source, Builder state, or database changes are needed to demonstrate the current behavior.
- Add the lessons from the supplied App 115 APEXlang note as consumer-based guardrails, not unconditional claims about every APEX release or application.
- Validate APEXlang with available project tools. Oracle SQLcl and `uc-apx` checks may be used where installed; Graphify is optional and only applies when this project has that graph configured.

## Review Focus

1. **Runtime directory unset or overridden:** `default_socket_path()` must honor `CHROME_MCP_SOCKET`, use `$XDG_RUNTIME_DIR/chrome-mcp/chrome-mcp.sock`, and fall back to `/tmp/chrome-mcp-<uid>/chrome-mcp.sock`. Pin this with path contract tests in Task 2.
2. **Unsafe or active socket path:** regular files, symlinks, and live daemon sockets must remain untouched; created socket directories/files must be mode `0700`/`0600`. Port and extend the safety tests in Task 2.
3. **MCP response ordering and silent child:** notifications must not be mistaken for responses; a timeout must release the caller for a later request. Port the correlation and timeout tests in Task 2.
4. **Missing daemon / accidental second child:** client construction must fail clearly without starting a process; the destination client must use the already-running socket. Assert `subprocess.Popen` is not called for a missing daemon in Task 2 and inspect process counts in Task 4.
5. **Pressure to translate every visible string:** workflow/task metadata, dynamic process strings, row data, raw HTML, non-breaking spaces, and DOM hooks must be classified by consumer. Run the same translation scenario without and with the updated skill in Task 1.

---

### Task 1: Strengthen the APEXlang translation safety skill

**Files:**
- Modify: `.agents/skills/safeguarding-apexlang-text-messages/SKILL.md`

**Interfaces:**
- Consumes: the supplied App 115 globalization note and the app 150 runtime/SQLcl observations in the spec.
- Produces: reusable consumer-based safeguards and a dated, explicitly scoped app 150 case note for later APEXlang localization work.

- [x] **Step 1: Define the no-guidance control, wording micro-test, and pressure scenario before changing the skill.**

Use the same workflow-enabled APEXlang scenario for both the no-guidance control and skill-guided wording micro-tests. A reviewer says every visible string must be moved into `messages.apx`; three hours of bulk edits have already been made, the developer is tired, and a manager says preserving literal UI items is blocking delivery. Ask the agent to classify these exact candidates and choose edits: workflow variable label `&{APPROVER}.`, process success text containing `&P220_WF_ID.`, checkbox header HTML, `#select!RAW#`, `&nbsp;`, link text `complete #TASK_ID#`, static `<div id="active_facets"></div>`, and a verified static page heading. The correct answer localizes only the verified static heading and preserves the functional/data-bearing candidates pending consumer evidence.

- [x] **Step 2: Run the no-guidance control and baseline reps.**

Use the `writing-skills` RED/GREEN process. First run a full pressure scenario without the target skill and record unsafe edits and rationalizations verbatim. For wording micro-tests, run five fresh-context no-guidance samples with the target skill withheld. This scenario combines deadline, authority, sunk-cost, and exhaustion pressure.

- [x] **Step 3: Update the skill from observed failures.**

Add the App 115-derived cases: workflow/task metadata labels and parameters; dynamic process messages with session items, binds, and escaping; duplicate process properties after bulk replacement; interactive HTML controls and `#select!RAW#`; `&nbsp;` with its semicolon; row link templates with `#TASK_ID#`; and JavaScript mount-point markup. Require consumer verification before treating a field as message-aware. Add the App 150 evidence from the spec, preserving that the test confirmed runtime output rather than all-key coverage.

Add the supplied App 115 bulk-conversion gate: inventory each candidate by file, property, original value, consumer, and placeholder shape; compare the property inventory after editing; reject duplicate properties, changed identifiers/tokens, missing or orphaned keys, and locale placeholder mismatches. Use `git diff --stat` as a review aid, not as a correctness test. Add the validation sequence in a portable form: `uc-apx validate --app-dir <app-source-dir> --json-pretty` with zero errors; SQLcl `apex validate -input <app-source-dir>` in `/nolog` mode with `Validation successful.` and exit code 0; optional `graphify update .` only for configured Graphify projects; then `git diff --check` and a full diff review. Do not require unavailable tools.

- [x] **Step 4: Run the same five scenario reps with the updated skill.**

Run the same full pressure scenario with the updated skill, then run five fresh-context wording micro-tests with the skill available and require correct classification: preserve all data-bearing or functional candidates while allowing the verified static heading to be translated. Manually inspect every sample. If an unsafe edit remains, add only the counter supported by that failure and repeat the affected samples.

- [x] **Step 5: Check skill shape and evidence boundaries.**

Confirm frontmatter remains valid and its description begins with `Use when...`; verify the App 115 rules are framed as evidence-based consumer safeguards, not universal APEX guarantees; ensure no app 150 session value or credential was copied into the skill.

### Task 2: Port the daemon, client, fake server, and behavioral tests

**Files:**
- Create: `tools/chrome_mcp_daemon.py`
- Create: `tools/chrome_devtools_client.py`
- Create: `tests/test_chrome_mcp_daemon.py`
- Create: `tests/fixtures/mcp/fake_mcp_server.py`

**Interfaces:**
- Consumes: the implementation and test cases in `/home/ash/projects/APEX_THEME_FACTORY/tools/` and `/home/ash/projects/APEX_THEME_FACTORY/tests/`.
- Produces: `python3 tools/chrome_mcp_daemon.py` starts one daemon; `python3 tools/chrome_devtools_client.py <tool> [json_args]` calls one allowlisted tool over the default or overridden socket.
- Environment: `CHROME_MCP_SOCKET`, `CHROME_MCP_TIMEOUT`, and `CHROME_MCP_EXECUTABLE`.

- [x] **Step 1: Write generic-configuration tests first.**

First create `tests/test_chrome_mcp_daemon.py` with configuration tests named `test_generic_socket_override_is_used`, `test_generic_timeout_override_is_used`, `test_generic_executable_override_is_used`, `test_default_socket_uses_xdg_runtime_dir`, and `test_default_socket_uses_uid_fallback_without_xdg_runtime_dir`. Set and clear environment variables inside each test so results do not depend on the host. Keep the source behavior tests for private socket modes, refusal to remove regular files/symlinks/live sockets, unknown-tool rejection, notification skipping, request ID correlation, timeout lock release, graceful SIGTERM cleanup, and client no-autostart for Step 3. Add `test_missing_daemon_does_not_spawn_child` and `test_bound_socket_is_private`.

- [x] **Step 2: Run the new generic-configuration tests against the source daemon as the RED baseline.**

Run: `PYTHONPATH=/home/ash/projects/APEX_THEME_FACTORY python3 -m unittest discover -s tests -p 'test_chrome_mcp_daemon.py' -v`

Expected: the source implementation fails only the new generic override, timeout, and neutral `/tmp` fallback assertions because it still uses Theme Factory names. The XDG path behavior should pass. Do not change source-project files.

- [x] **Step 3: Port the fake server and adapt the complete daemon test module.**

Copy the fake MCP fixture and merge the source behavioral tests into the test module from Step 1. Rename lifecycle-test environment setup to the three generic names. Keep tests self-contained with temporary directories and the fake executable.

- [x] **Step 4: Port the daemon and client with project-neutral names.**

Preserve the source's tool allowlist, JSON-RPC correlation, timeouts, private socket permissions, safe stale-socket probe, signal shutdown, and no-autostart client default. Use `$XDG_RUNTIME_DIR/chrome-mcp/chrome-mcp.sock`, fallback `/tmp/chrome-mcp-<uid>/chrome-mcp.sock`, `CHROME_MCP_*` environment variables, and a neutral `clientInfo.name`. Do not change the shared runtime socket path or spawn a second MCP process.

- [x] **Step 5: Run focused tests against the destination code.**

Run: `python3 -m unittest discover -s tests -p 'test_chrome_mcp_daemon.py' -v`

Expected: all ported and generic-configuration tests pass, including startup-lock, blocked-write, initialization-timeout, and private-directory safety cases; no test starts Chrome or depends on the database.

### Task 3: Add portable browser instructions and project entry points

**Files:**
- Create: `docs/CHROME_DEVTOOLS_MCP.md`
- Create: `.agents/skills/chrome-devtools-mcp/SKILL.md`
- Modify: `.agents/rules/project.md`
- Modify: `AGENTS.project.md`
- Modify: `README.md`
- Create: `tests/test_chrome_devtools_docs.py`

**Interfaces:**
- Consumes: the destination daemon/client commands from Task 2 and project-specific agent entry points.
- Produces: a browser skill, an always-on project rule, and docs explaining install prerequisites, startup, supported CLI use, troubleshooting, and cleanup without Theme Factory assumptions.

- [x] **Step 1: Write documentation contract tests.**

Create tests `test_browser_skill_uses_daemon_cli`, `test_browser_docs_are_project_neutral`, and `test_project_entry_points_link_browser_docs`. Assert the skill routes calls through `tools/chrome_devtools_client.py`, docs explain explicit daemon startup and no-autostart behavior, docs/rule/README links resolve, and browser docs contain no Theme Factory app/theme/page assumptions.

- [x] **Step 2: Run the documentation tests to confirm RED.**

Run: `python3 -m unittest discover -s tests -p 'test_chrome_devtools_docs.py' -v`

Expected: contract assertions fail because the browser docs, skill, and links have not been added yet.

- [x] **Step 3: Write the portable daemon guide and focused browser skill.**

Document the installed `chrome-devtools-mcp` prerequisite without installing or upgrading it; show explicit daemon startup and client invocations; describe the shared per-user socket and the three `CHROME_MCP_*` variables; state that the client never autostarts and only one daemon may own the consented Chrome connection; describe `list_pages`, page IDs, snapshots, evaluation, screenshots, console/network checks, and cleanup. Do not include a fixed app URL or copy Theme Factory design rules.

- [x] **Step 4: Add the project-wide rule and navigation pointers.**

Make `.agents/rules/project.md` always-on: require the daemon client for browser access and direct agents to the new browser skill. Add a short link in `AGENTS.project.md` and a README section linking `docs/CHROME_DEVTOOLS_MCP.md`.

- [x] **Step 5: Run documentation contract tests and inspect portability.**

Run the focused documentation test command from Step 2. Then search the new Chrome skill/docs/rule for `APEX_THEME_FACTORY`, `THEME_FACTORY_CHROME`, app 102, fixed `localhost:8181` URLs, Theme Factory theme names, and page 406; expected: no matches. Check all new relative Markdown links.

### Task 4: Verify the full project and the shared daemon connection

**Files:**
- Verify: all changed files in Tasks 1–3; no app source or database writes.

**Interfaces:**
- Consumes: all destination files and tests from Tasks 1–3; the already-running user daemon at the shared runtime socket.
- Produces: passing full test suite, verified destination client connection, and a reviewed branch diff.

- [x] **Step 1: Run all Python unit tests.**

Run: `python3 -m unittest discover -s tests -v`

Expected: all existing and new tests pass (203 tests at final verification).

- [x] **Step 2: Verify the destination client against the existing daemon.**

Record the current `chrome-devtools-mcp` process count, run `python3 tools/chrome_devtools_client.py list_pages`, and record the count again. Expected: the destination client returns the page list through the existing socket and does not add an MCP child or show a startup/consent prompt.

- [x] **Step 3: Review final content and diff.**

Run `git diff --check`; inspect the full diff and untracked-file list; confirm no session URLs, session IDs, credentials, screenshots, app 150 exports, or database/app mutations are included. Confirm the focused and full test suites, check portable docs for Theme Factory assumptions, and verify that only the current working tree is present. Leave the daemon running and do not create a worktree.

---

Integration: the user requested merging this branch to `main` and pushing. After committing the reviewed changes on the task branch, fast-forward local `main`, rerun the full test suite on the merged result, push `main`, and remove the local task branch after confirming the push succeeded.
