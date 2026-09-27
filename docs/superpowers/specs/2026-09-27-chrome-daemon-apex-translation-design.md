# Chrome MCP Daemon and APEX Translation Verification — Design

**Date:** 2026-09-27
**Status:** Planning authorized by user's instruction to create a plan and implement it
**Branch:** `codex/chrome-daemon-apex-translation-live-check`

## 1. Goal and constraints

Add the reusable Chrome DevTools MCP daemon workflow from
`/home/ash/projects/APEX_THEME_FACTORY/` to this APEX team template, with the
Theme Factory-specific app, theme, URL, and environment assumptions removed.
Use the daemon to verify app 150 as `ROBERT` and preserve the observed APEXlang
translation lessons in the existing `safeguarding-apexlang-text-messages`
skill.

Constraints:

- Use a normal Git branch; do not create a worktree.
- Run browser calls only through `tools/chrome_devtools_client.py` and the
  persistent daemon. The client must not silently start a daemon.
- Keep the socket in the existing per-user runtime location so projects under
  this account can share the one consented Chrome session. Use generic
  environment variable names and a generic `/tmp` fallback.
- Do not store credentials, session URLs, or session identifiers in the
  repository or final report.
- The app 150 check is read-only. No app source, Builder state, or database
  changes are needed to demonstrate the current behavior.

## 2. Approaches considered

1. **Minimal portable daemon port (selected).** Copy the daemon/client, focused
   tests and fake MCP server, and rewrite the docs and skill for this template.
   Add a project-wide browser rule and a README pointer. Keep the tool
   allowlist, socket safety, explicit startup, request timeouts, and shutdown
   guarantees.
2. **Copy the Theme Factory browser environment.** This would also pull in its
   theme documentation, app 102 assumptions, CSS workflow, and generated-file
   rules. Those do not belong in this team template.
3. **One-off browser access.** This would provide a single check but would not
   add the reusable daemon and instructions requested.

Option 1 is the smallest complete port that supports the requested live check.

## 3. Files and behavior

Port or add:

- `tools/chrome_mcp_daemon.py`
- `tools/chrome_devtools_client.py`
- `tests/test_chrome_mcp_daemon.py`
- `tests/fixtures/mcp/fake_mcp_server.py`
- `docs/CHROME_DEVTOOLS_MCP.md`
- `.agents/skills/chrome-devtools-mcp/SKILL.md`
- `.agents/rules/project.md` browser-daemon rule
- a browser-tooling pointer in `AGENTS.project.md` and `README.md`

The daemon remains explicitly started with
`python3 tools/chrome_mcp_daemon.py`. The client exposes only allowlisted MCP
tools and returns an error if the daemon is unavailable. Socket directories
and sockets stay private to the current user; stale sockets are replaced only
after a connection probe; active sockets are never replaced. Request IDs are
correlated and unanswered requests time out without blocking later callers.

Rename Theme Factory variables and client identity to project-neutral names.
Keep the shared `$XDG_RUNTIME_DIR/chrome-mcp/chrome-mcp.sock` path, with a
neutral fallback such as `/tmp/chrome-mcp-<uid>/chrome-mcp.sock`. The skill
documents the explicit start command, startup consent, owned background tabs,
page-scoped tool arguments, and tab cleanup. It does not prescribe a fixed
application URL or assume a particular app, workspace, theme, or page.

The tests use the existing `unittest` convention and fake MCP process to check
socket permissions, stale-socket safety, tool allowlisting, request correlation
and timeouts, shutdown cleanup, and the client's no-autostart behavior.

## 4. Translation-safety skill update

Add the lessons from the supplied App 115 APEXlang note as consumer-based
guardrails, not unconditional claims about every APEX release or application:

- Treat workflow variables/parameters and task-definition names/parameters as
  runtime metadata. Keep stable identifiers literal unless the exact runtime
  consumer is shown to evaluate APEX text messages.
- Keep dynamic process messages containing `&ITEM.`, binds, or escaping
  directives literal unless the caller/consumer and placeholder contract are
  proven. Check that a conversion did not leave duplicate properties.
- Preserve interactive header HTML, `#select!RAW#`, row-driven link text such
  as `complete #TASK_ID#`, and JavaScript mount-point HTML as executable or
  data-bearing content.
- Preserve `&nbsp;` as a literal HTML entity with its semicolon.
- For message placeholders such as `%name` and `%0`, verify that the caller
  supplies the argument and preserve placeholder names/counts across locales.
- Validate APEXlang with available project tools. Oracle SQLcl and `uc-apx`
  checks may be used where installed; Graphify is optional and only applies
  when this project has that graph configured. Review the full diff for
  duplicate properties, changed identifiers, and unresolved message keys.

Retain the existing Oracle guidance on compatibility mode and `&{KEY}.` versus
legacy `&APP_TEXT$KEY.` syntax. Do not assume that visibility in a rendered
page means a field is message-aware.

## 5. App 150 live-check evidence

The user authorized a live check as `ROBERT`. The project has no local source
tree for app 150, so the check used the existing APEX runtime via an owned
background Chrome tab and made no app or database writes.

Observed live metadata: app 150 is `workflow-showcase`, APEX 26.1.4 with
compatibility mode 26.1, uses Text Messages, and derives language from session.
SQLcl comparison found 142 Arabic/English message-key pairs with identical
text.

Runtime comparison:

- Home page 1 switched from English to Arabic: the Arabic document reported
  `lang=ar` and `dir=rtl`, while its title/heading stayed `workflow-showcase`.
- Page 6 reported `lang=ar` and `dir=rtl` in Arabic mode, then `lang=en` in
  English mode. Its title/heading was `MY TASKS` in both modes.
- In both page 6 modes, a task card visibly contained the literal
  `&{REQUESTAPPROVAL}.` key. This is runtime evidence that this field is not
  being evaluated as an application text message in that consumer.
- The browser console returned no messages during the check.

These results show that session locale switching and RTL direction work, while
the inspected text-message content is not localized and a task-card value
contains an unevaluated key. They do not establish that all 142 keys are used
by page-rendered components. Preserve these observations as a dated test case;
do not present them as a universal APEX rule.

## 6. Acceptance criteria and boundaries

- The destination daemon/client run against the shared user socket without
  creating a second MCP child when the daemon is already active.
- Daemon unit tests pass, including explicit client no-autostart and socket
  cleanup safety.
- The project browser skill/rule describes only project-neutral usage and
  follows the daemon-only access path.
- The translation skill contains the App 115-derived consumer safeguards and
  the App 150 live evidence above, without asserting unsupported universal
  behavior.
- The live app 150 result remains read-only; no import, migration, publish,
  Builder edit, or app-source change is part of this work.
- No session identifier or credential is written to docs, tests, or logs.

## 7. Risks and limitations

The local checkout has no app 150 APEXlang source, so this task cannot make or
verify a declarative app fix. The live task-card text is concrete evidence of
an unevaluated message key, but resolving it requires reviewing the actual
task-definition consumer and coordinating any shared Builder/import change
under the repository's team workflow. A Git branch does not isolate the shared
APEX workspace or database.
