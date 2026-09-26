---
name: apex-session-context
description: Use when SQL or PL/SQL outside a normal APEX page request reads task or workflow views, or calls APEX workflow APIs, especially from SQLcl, a scheduler job, or an automation.
---

# Establish APEX context before task and workflow operations

An empty result from an APEX task or workflow view can mean that the current
database session lacks the right APEX workspace or application context. Check
the context before treating an empty result as evidence that the task or
workflow does not exist.

## When this applies

- PL/SQL reads `APEX_TASKS`, `APEX_TASK_PARAMETERS`, `APEX_WORKFLOWS`, or
  another APEX-owned view whose rows depend on workspace or session context.
- PL/SQL calls an APEX workflow API from SQLcl, a database scheduler job, an
  automation, or another non-page request.
- A retry loop reports no task or workflow even though another authorized
  source shows that the instance exists.

For page-item binds and substitution strings in background APEX execution,
also use `apex-background`. This skill is about metadata visibility and API
preconditions.

## Check the context first

Before changing code, run a narrow, read-only check for the target app and
instance. Record the database identity and the APEX context available to that
session, including `NV('FLOW_SECURITY_GROUP_ID')`, `V('APP_ID')`, and
`V('APP_SESSION')` where those functions are available. In two source
projects, task and workflow view lookups returned no rows when
`FLOW_SECURITY_GROUP_ID` was null or zero, although the instances existed.
Repeating the same lookup did not restore visibility.

For SQLcl preflights that inspect `APEX_WORKSPACES` or `APEX_APPLICATIONS`,
resolve the requested workspace with `APEX_UTIL.FIND_SECURITY_GROUP_ID`,
require a positive result, then call `APEX_UTIL.SET_SECURITY_GROUP_ID` before
querying those views. Compare `NV('FLOW_SECURITY_GROUP_ID')` with the resolved
ID and confirm the same ID and name are visible in `APEX_WORKSPACES`. If either
context check fails, abort; never interpret an empty application query as
proof that an app ID is unused.

Treat this observation as a diagnostic clue, not a universal promise about
every APEX view. Inspect the view and API requirements for the deployed APEX
release and the correct workspace/application.

## Establish only the context the operation requires

- For a workspace-scoped operation that does not require an application
  session, resolve the correct workspace and use the project's established
  workspace-context pattern. Oracle documents `APEX_UTIL.FIND_SECURITY_GROUP_ID`
  with `APEX_UTIL.SET_SECURITY_GROUP_ID` for batch use.
- For an API that requires a valid APEX session, use the approved bootstrap
  for this project. Oracle documents `APEX_SESSION.CREATE_SESSION` with the
  exact application ID, page ID, and session username. It runs the
  application's Initialization PL/SQL Code, so inspect that code before
  creating a session from a job or SQLcl.
- Do not invent a workspace, application, user, or project bootstrap package.
  Do not swallow a bootstrap error and then continue as though the context
  exists.

`APEX_WORKFLOW` requires a valid APEX session. Setting a workspace security
group is not a substitute for a full application session when an API requires
one.

## Verify visibility

After establishing context, repeat the same read-only query with a known
instance identifier and the intended application/workspace predicates. If it
still returns no row, stop and inspect the target context, authorization,
view filters, and identifier before retrying or mutating anything. Do not use a
missing-context result as a reason to complete, cancel, or recreate a task.

## References

- [APEX_SESSION API](https://docs.oracle.com/en/database/oracle/apex/26.1/aeapi/APEX_SESSION.html)
- [APEX_SESSION.CREATE_SESSION](https://docs.oracle.com/en/database/oracle/apex/26.1/aeapi/CREATE_SESSION-Procedure.html)
- [APEX_UTIL.FIND_SECURITY_GROUP_ID](https://docs.oracle.com/en/database/oracle/apex/26.1/aeapi/FIND_SECURITY_GROUP_ID-Function.html)
- [APEX_UTIL.SET_SECURITY_GROUP_ID](https://docs.oracle.com/en/database/oracle/apex/26.1/aeapi/SET_SECURITY_GROUP_ID-Procedure.html)
- [APEX_WORKFLOW API](https://docs.oracle.com/en/database/oracle/apex/26.1/aeapi/APEX_WORKFLOW.html)
