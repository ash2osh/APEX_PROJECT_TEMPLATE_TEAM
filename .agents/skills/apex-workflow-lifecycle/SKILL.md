---
name: apex-workflow-lifecycle
description: Use when importing or moving an APEX app with live workflow or task instances, changing app automations, diagnosing a missing schedule, or running bulk workflow or task operations.
---

# Protect live APEX workflow and automation state

Application source and live workflow state are separate. Before an import,
move, resume, termination, or automation change, record the current state and
verify the result in the target APEX release. Oracle's general workflow
documentation does not replace project-specific checks on live instances.

## Before an import or application move

1. Follow the repository's export, publish, and deployment rules. Do not
   bypass its import guard.
2. Use read-only queries to record workflow counts by state, task counts by
   state, and automation status for the target application. Inspect the
   installed APEX version and the relevant view columns first; metadata-view
   access can depend on APEX session context, so use `apex-session-context`.
3. Import over the existing application when preserving live workflow and
   task instances. Oracle documents that deleting the application deletes
   those instances.
4. After import, re-check workflow counts and audit history, then inspect
   automation status and recent execution logs. Oracle documents that
   automations are disabled after import and that running workflows are
   suspended during reimport before automatic resumption. Verify both results
   in the actual environment; do not blindly enable automations or resume
   every suspended instance.

## When moving an automation to another application

Review the query and actions for app-scoped references before enabling the
copy:

- `:APP_ID`, `V('APP_ID')`, application items, and page items;
- message substitutions such as `&{MESSAGE_KEY}.`;
- task definitions, workflow definitions, static IDs, and error handling;
- APEXlang execution settings that mark an automation active.

Project evidence showed that `APP_ID` resolved to the destination (host)
application after a move, and that app-owned messages and task references did
not automatically move with the automation. Run the copied query read-only in
the destination context, compare the returned rows with the source
application, and confirm that the copied automation's enabled state and error
handling are intentional before enabling it.

## When resuming or terminating many instances

- Inspect each workflow's state, current activity, related tasks, and audit
  history before choosing an operation. `RESUME` applies to suspended
  workflows; it is not a repair for an arbitrary inconsistent task list.
- Start with a small, representative batch in a non-production environment.
  Record outcomes only after the transaction boundary and audit state are
  confirmed; a state read inside a loop is not proof that the operation was
  committed.
- Source-project evidence showed that one failed resume in a bulk operation
  rolled back earlier work in that transaction. Confirm the behavior of the
  target APEX release and calling code before choosing per-instance commits.
  An explicit `COMMIT` in a helper can also commit its caller's work, so inspect
  the complete call chain first.
- Do not cancel a task as a de-duplication shortcut. Confirm how the task
  operation affects its parent workflow in the target release and application.

## When a scheduled job appears missing

Check both APEX Automations and `DBMS_SCHEDULER`. A recurring process may be
defined as an APEX Automation and will not necessarily appear as a
project-owned database scheduler job. Confirm the automation's status,
polling details, and execution log before concluding that it is absent or did
not run.

## Completion checks

- Compare workflow and task state counts before and after the operation.
- Review workflow audit rows and automation execution logs for the expected
  transitions and errors.
- For a moved automation, compare its read-only query result and a successful
  execution in the destination application.
- Report unverified runtime state as unknown; do not infer success from an
  import exit code or an in-loop message.

## References

- [APEX_WORKFLOW API](https://docs.oracle.com/en/database/oracle/apex/26.1/aeapi/APEX_WORKFLOW.html)
- [Workflow reimport FAQ](https://docs.oracle.com/en/database/oracle/apex/26.1/htmdb/workflow-faqs.html)
- [Importing vs. deleting an application](https://docs.oracle.com/en/database/oracle/apex/26.1/apxdc/importing-vs-deleting-application.html)
- [Managing automations](https://docs.oracle.com/en/database/oracle/apex/26.1/htmdb/managing-automations.html)
