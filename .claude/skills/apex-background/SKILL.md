---
name: apex-background
description: Use when writing or debugging Oracle APEX PL/SQL in automations, workflow activities, human task actions, or background execution chains to check session state, bind variables, and substitution strings. Probe evidence is from APEX 26.1.4.
---

# APEX background execution contexts

Use the exact execution context when deciding whether an APEX session, page
item, bind variable, or substitution string is available. The probe matrix in
`.agents/skills/apex-background/findings-apex-26.1.md` was collected on APEX
26.1.4, database FREEPDB1, SQLcl 26.2.2.233.1901, on 2026-09-26. Values are
observations from the named probe; they are not promises for other versions,
apps, or execution paths. `<null>` is a returned null; `-` means that the
probe did not record that value in the context.

## Session observations

In every sampled context where `V(APP_SESSION)` was probed, it was non-null
except for the explicit `SQLCL_NO_SESSION` control. `TASK_ACTION_COMMENT` only
captured task text, so its general session values are not verified.

| Context | `V(APP_SESSION)` | `V(APP_USER)` | `V(PROBE_APP_ITEM)` / `V(P1_PROBE_ITEM)` | `USERENV BG_JOB_ID` |
| --- | --- | --- | --- | --- |
| SQLCL_NO_SESSION | `<null>` | `<null>` | `<null>` / `<null>` | `<null>` |
| SQLCL_APEX_SESSION | set | `PROBE_USER` | `app-item-from-sqlcl` / `page-item-from-sqlcl` | `<null>` |
| AUTOMATION_ON_DEMAND | set | `PROBE_USER` | `app-item-from-sqlcl` / `page-item-from-sqlcl` | `<null>` |
| AUTOMATION_ON_DEMAND_BACKGROUND | set | `nobody` | `<null>` / `<null>` | `82779` |
| AUTOMATION_SCHEDULED | set (3 runs) | `nobody` | `<null>` / `<null>` | `82781`, `82783`, `82786` |
| WORKFLOW_START | set | `nobody` | `<null>` / `<null>` | `82780` |
| WORKFLOW_AFTER_WAIT | set | `nobody` | `<null>` / `<null>` | `82785` |
| WORKFLOW_AFTER_TASK | set | `nobody` | `<null>` / `<null>` | `82788` |
| TASK_ACTION_CREATE | set (2 calls) | `PROBE_USER`, `nobody` | set then `<null>` / set then `<null>` | `<null>`, `82785` |
| TASK_ACTION_COMPLETE | set (2 calls) | `PROBE_USER` (both) | `<null>` / `<null>` | `<null>` (both) |
| TASK_ACTION_COMMENT | not verified on 26.1 | not verified on 26.1 | not verified on 26.1 | not verified on 26.1 |
| PAGE_PROCESS_FOREGROUND | set | `nobody` | `<null>` / `page-item-value` | `<null>` |
| EXECUTION_CHAIN_BACKGROUND | set | `nobody` | `<null>` / `page-item-value` | `82782` |

The run shows that an APEX session ID can exist while `APP_USER` is `nobody`.
Do not treat a non-null `V(APP_SESSION)` as proof of a human login or as an
authorization check. The chain retained the submitted page item in this probe,
but workflow activities and most automation/task executions returned null for
the test page item. Pass required values explicitly instead of relying on
ambient page state.

`&APP_TITLE.` resolved to `APEX Background Probe`, and the application
substitution `&PROBE_SUBST.` resolved to `probe-subst-value` in the captured
APEX contexts; both returned `<null>` in `SQLCL_NO_SESSION`. The application
item `PROBE_APP_ITEM` was set only in the SQLcl-created session, the foreground
on-demand automation, and one task-create call. `P1_PROBE_ITEM` was set in the
SQLcl session, foreground on-demand automation, one task-create call, the page
process, and the background execution chain. Consult the matrix for each
individual official substitution result. A null result is not evidence that a
name is unsupported in every APEX context.

For a diagnostic in a specific APEX component, probe the value there and
retain its context name:

```plsql
DECLARE
  l_title VARCHAR2(4000);
BEGIN
  l_title := apex_application.do_substitutions('&APP_TITLE.');
  -- Record l_title alongside the actual automation, workflow, or task context.
END;
```

Treat a null as that context's result. Do not use substitution output as an
authorization check.

## Bind variables with observed values

These bind names had at least one non-null observation. The matrix also records
official binds that returned null, including `APEX$WORKFLOW_CREATED_ON`,
`APEX$TASK_DUE_ON`, `APEX$TASK_MAX_RENEWAL_COUNT`, and
`APEX$TASK_PREVIOUS_ID`.

| Bind | Contexts with a value |
| --- | --- |
| `:APP_ID` | AUTOMATION_ON_DEMAND, AUTOMATION_ON_DEMAND_BACKGROUND, AUTOMATION_SCHEDULED |
| `:APP_USER` | AUTOMATION_ON_DEMAND, TASK_ACTION_CREATE, AUTOMATION_ON_DEMAND_BACKGROUND, WORKFLOW_START, AUTOMATION_SCHEDULED, EXECUTION_CHAIN_BACKGROUND, TASK_ACTION_COMPLETE, WORKFLOW_AFTER_TASK |
| `:APP_SESSION` | AUTOMATION_ON_DEMAND, TASK_ACTION_CREATE, AUTOMATION_ON_DEMAND_BACKGROUND, WORKFLOW_START, AUTOMATION_SCHEDULED, EXECUTION_CHAIN_BACKGROUND, TASK_ACTION_COMPLETE, WORKFLOW_AFTER_TASK |
| `:PROBE_APP_ITEM` | AUTOMATION_ON_DEMAND |
| `:APEX$WORKFLOW_ACTIVITY_ID` | WORKFLOW_START, WORKFLOW_AFTER_WAIT, WORKFLOW_AFTER_TASK |
| `:APEX$WORKFLOW_ID` | WORKFLOW_START, WORKFLOW_AFTER_WAIT, WORKFLOW_AFTER_TASK |
| `:APEX$WORKFLOW_DETAIL_PK` | WORKFLOW_START, WORKFLOW_AFTER_WAIT, WORKFLOW_AFTER_TASK |
| `:APEX$WORKFLOW_INITIATOR` | WORKFLOW_START, WORKFLOW_AFTER_WAIT, WORKFLOW_AFTER_TASK |
| `:APEX$WORKFLOW_STATE` | WORKFLOW_START, WORKFLOW_AFTER_WAIT, WORKFLOW_AFTER_TASK |
| `:P_PROBE_PARAM` | WORKFLOW_START |
| `:PROBE_DATA` | WORKFLOW_START |
| `:APEX$TASK_CREATED_ON` | TASK_ACTION_CREATE, TASK_ACTION_COMPLETE |
| `:APEX$TASK_ID` | TASK_ACTION_CREATE, TASK_ACTION_COMPLETE |
| `:APEX$TASK_INITIATOR` | TASK_ACTION_CREATE, TASK_ACTION_COMPLETE |
| `:APEX$TASK_OWNER` | TASK_ACTION_CREATE, TASK_ACTION_COMPLETE |
| `:APEX$TASK_PK` | TASK_ACTION_CREATE, TASK_ACTION_COMPLETE (null for the workflow-created task) |
| `:APEX$TASK_RENEWAL_COUNT` | TASK_ACTION_CREATE, TASK_ACTION_COMPLETE |
| `:APEX$TASK_STATE` | TASK_ACTION_CREATE, TASK_ACTION_COMPLETE |
| `:APEX$TASK_SUBJECT` | TASK_ACTION_CREATE, TASK_ACTION_COMPLETE |
| `:APEX$TASK_OUTCOME` | TASK_ACTION_COMPLETE |
| `:APEX$TASK_TEXT` | TASK_ACTION_COMMENT (three Add Comment calls; task IDs are retained in the findings rows) |
| `:T_PROBE_PARAM` | TASK_ACTION_CREATE, TASK_ACTION_COMPLETE |
| `:P1_PROBE_ITEM` | EXECUTION_CHAIN_BACKGROUND |
| `:TASK_OUTCOME` | WORKFLOW_AFTER_TASK |

The six official workflow names are `APEX$WORKFLOW_ACTIVITY_ID`,
`APEX$WORKFLOW_CREATED_ON`, `APEX$WORKFLOW_DETAIL_PK`, `APEX$WORKFLOW_ID`,
`APEX$WORKFLOW_INITIATOR`, and `APEX$WORKFLOW_STATE`. The probe observed
`APEX$WORKFLOW_CREATED_ON` as null in all three workflow activities. Use
workflow parameters and workflow binds for workflow data; do not assume page
items will carry through a workflow wait or task boundary.

The thirteen official task names are `APEX$TASK_CREATED_ON`, `APEX$TASK_DUE_ON`,
`APEX$TASK_ID`, `APEX$TASK_INITIATOR`, `APEX$TASK_MAX_RENEWAL_COUNT`,
`APEX$TASK_OUTCOME`, `APEX$TASK_OWNER`, `APEX$TASK_PK`,
`APEX$TASK_PREVIOUS_ID`, `APEX$TASK_RENEWAL_COUNT`, `APEX$TASK_STATE`,
`APEX$TASK_SUBJECT`, and `APEX$TASK_TEXT`. Task values can differ between
standalone and workflow-created tasks; check the actual bind in the relevant
task action. Oracle documents `APEX$TASK_TEXT` for Add Comment, Request
Information, and Submit Information. This probe observed Add Comment only;
Request Information and Submit Information are not verified here.

## Official substitution strings and scope

Oracle's [26.1 built-in substitution reference](https://docs.oracle.com/en/database/oracle/apex/26.1/htmdb/using-available-built-in-substitution-strings.html)
lists these general names. Scope still matters: several are template tokens,
page or request values, or row-processing values rather than background PL/SQL
values.

```text
APEX_CSP_DISPLAY_NONE, APEX_FILES, APEX$ROW_NUM, APEX$ROW_SELECTOR, APEX$ROW_STATUS,
APP_ID, APP_ALIAS, APP_AJAX_X01, APP_AJAX_X02, APP_AJAX_X03, APP_AJAX_X04, APP_AJAX_X05,
APP_AJAX_X06, APP_AJAX_X07, APP_AJAX_X08, APP_AJAX_X09, APP_AJAX_X10, APP_BUILDER_SESSION,
APP_DATE_TIME_FORMAT, APP_FILES, APP_NLS_DATE_FORMAT, APP_NLS_TIMESTAMP_FORMAT,
APP_NLS_TIMESTAMP_TZ_FORMAT, APP_PAGE_ALIAS, APP_PAGE_ID, APP_REGION_DOM_ID, APP_REGION_ID,
APP_REGION_STATIC_ID, APP_REQUEST_DATA_HASH, APP_SESSION, SESSION, APP_SESSION_VISIBLE,
APP_TEXT$Message_Name, APP_TEXT$Message_Name$Lang, APP_TITLE, APP_UNIQUE_PAGE_ID, APP_USER,
APP_VERSION, AUTHENTICATED_URL_PREFIX, BROWSER_LANGUAGE, CURRENT_PARENT_TAB_TEXT, DEBUG,
DEFAULT_THEME_FILES, HOME_LINK, JET_BASE_DIRECTORY, JET_CSS_DIRECTORY, JET_JS_DIRECTORY,
LOGIN_URL, LOGOUT_URL, MAIN_APP_ID, OWNER, PRINTER_FRIENDLY, PROXY_SERVER, PUBLIC_URL_PREFIX,
REQUEST, SCHEMA OWNER, SQLERRM, SYSDATE_YYYYMMDD, THEME_DB_FILES, THEME_FILES,
WORKSPACE_FILES, WORKSPACE_ID
```

The probe exercised the documented general `&NAME.` forms in each context
where its session-capture procedure ran, plus the five supported legacy
aliases `APP_IMAGES`, `IMAGE_PREFIX`, `THEME_DB_IMAGES`, `THEME_IMAGES`, and
`WORKSPACE_IMAGE`. The per-name results are in the findings matrix. It also
included an actual text message to check `APP_TEXT$PROBE_MESSAGE` and
`APP_TEXT$PROBE_MESSAGE$EN`.

Keep the official scopes distinct:

- `APEX_CSP_DISPLAY_NONE`, `APP_VERSION`, `DEFAULT_THEME_FILES`,
  `JET_BASE_DIRECTORY`, `JET_CSS_DIRECTORY`, `JET_JS_DIRECTORY`, `OWNER`,
  `SQLERRM`, `THEME_DB_FILES`, and `THEME_FILES` are template tokens, not
  generic background PL/SQL variables.
- `APP_PAGE_*`, `APP_REGION_*`, `APP_AJAX_X01` through `APP_AJAX_X10`,
  `APP_REQUEST_DATA_HASH`, `APP_UNIQUE_PAGE_ID`, `CURRENT_PARENT_TAB_TEXT`,
  `REQUEST`, and the `APEX$ROW_*` names are page, request, or row-processing
  values. Their appearance in the official catalog does not make them
  meaningful in every background action.
- Oracle documents `SESSION` as an alias of `APP_SESSION`.
  `APP_REGION_STATIC_ID` is deprecated. `PROXY_SERVER` and `SCHEMA OWNER` are
  documentation headings; use `APEX_APPLICATION.G_PROXY_SERVER` and
  `APEX_APPLICATION.G_FLOW_SCHEMA_OWNER` in PL/SQL. `SYSDATE_YYYYMMDD` is
  exposed through `V`, a bind, or `G_SYSDATE`, not `&SYSDATE_YYYYMMDD.`.
- `APP_NAME` is not an official 26.1 built-in. Use `APP_TITLE`.

Oracle documents six [workflow substitution strings](https://docs.oracle.com/en/database/oracle/apex/26.1/htmdb/workflow-substitution-strings.html)
and thirteen [task substitution strings and bind variables](https://docs.oracle.com/en/database/oracle/apex/26.1/htmdb/substitution-strings-for-tasks.html).
For automation query values, use the documented query bind variables in
[Understanding Key Automation Concepts](https://docs.oracle.com/en/database/oracle/apex/26.1/apxdc/understanding-key-automation-concepts.html).
For a background page process, Oracle documents a private session with a copy
of page session state in [Understanding Background Page Processing](https://docs.oracle.com/en/database/oracle/apex/26.1/htmdb/understanding-background-page-processing.html).
Do not invent `APEX$AUTOMATION_*` or `APEX$BACKGROUND_*` names without an Oracle
reference.

The probe separately captured `APEX_APPLICATION.G_FLOW_ID`,
`G_FLOW_STEP_ID`, `G_INSTANCE`, `G_USER`, `G_FLOW_SCHEMA_OWNER`,
`G_PROXY_SERVER`, `G_SYSDATE`, and `V(SYSDATE_YYYYMMDD)`. These are APEX API or
view values, not extra substitution names; see their rows in the findings.

## Hypothesis checked

The hypothesis that background actions have no APEX session except task
actions was disproved for the sampled automation, workflow, and execution-chain
contexts: each returned a non-null `V(APP_SESSION)`. This does not imply a
logged-in user, durable page-item state, or equivalent behavior in another
APEX release. Generic session values for `TASK_ACTION_COMMENT` were not
captured. The test app exercised one task Add Comment action, not Request
Information or Submit Information.

## Common mistakes

- Assuming background means no session, or assuming a non-null session means a
  human user. Check both `V(APP_SESSION)` and `V(APP_USER)` in the exact context.
- Reading page items in a workflow or scheduled automation because one
  background execution-chain probe retained a page item. Prefer explicit
  workflow parameters, task binds, or documented automation query binds.
- Treating a null probe result as proof the token is unsupported everywhere.
  It only describes the recorded context and value at the time of the run.
- Treating every official catalog entry as a PL/SQL `&NAME.` substitution.
  Use each name's documented template, page, row, workflow, or task scope.
- Assuming `APEX$TASK_TEXT` was tested for every task action. Only Add Comment
  was exercised.
