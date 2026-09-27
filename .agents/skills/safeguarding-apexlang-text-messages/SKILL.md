---
name: safeguarding-apexlang-text-messages
description: Use when localizing, bulk converting, reviewing, or repairing APEXlang `textMessages`, especially with APEX substitutions, row tokens, workflow/task metadata, HTML controls, or JavaScript mount points.
---

# Safeguarding APEXlang text messages

Classify values by runtime consumer, not visibility. A message key is safe only when that field evaluates text messages.

**REQUIRED BACKGROUND:** Use `apexlang` for canonical syntax and artifacts; this skill narrows its translation workflow for runtime data and executable markup.

## Classify before converting

Read `application.apx`, the target components, `messages.apx`, and SQL/code or runtime views consuming candidates. Record each field and its consumer before editing.

| Candidate | Decision |
|---|---|
| Static APEX-rendered title, region name, button label, or report heading | Eligible for a shared message using canonical syntax. |
| Workflow/task names, parameter/variable labels, metadata surfaced by workflow/task views or SQL | Keep literal unless Oracle documentation and runtime evidence prove this exact field evaluates messages. UI visibility alone is not proof. |
| Process/task message containing `&ITEM.`, `!HTML`, `!RAW`, binds, or `%n` | Keep the entire value literal. Copying tokens into translations does not prove safe substitution. |
| Row link text containing `#COLUMN#`, such as `complete #TASK_ID#` | Keep all of `linkText`; do not extract and translate only its verb. |
| Functional source: checkbox HTML, `#select!RAW#`, JS hooks, DOM mount points (`<div id="active_facets"></div>`), SQL/PLSQL/JS/CSS, or raw plugin source such as `&{PLUG_SOURCE_2}!RAW.` | Preserve structure and hooks; do not make a message key for them. Isolate separate user-facing copy before translating it. |
| Spacing placeholder `&nbsp;` or empty content | Keep literal `&nbsp;` with the semicolon; leave empty content empty. |

## Safe conversion

1. Convert only static copy in verified message-aware fields. Use the current Oracle APEXlang syntax; do not reuse historical `&{KEY}.` syntax where the workflow specifies `&APP_TEXT$KEY.`.
2. Diff each changed field against the original; preserve exclusions, verify variants and references, and remove orphan keys.
3. If the consumer is unclear, leave it unchanged and unresolved; do not claim full localization. Validate per project workflow; import/export only if authorized.

```apexlang
label: &APP_TEXT$SAVE.
successMessage: "Task delegated to &P7_NEW_OWNER!HTML."
linkText: complete #TASK_ID#
```

## Rationalizations to reject

| Temptation | Response |
|---|---|
| “It is visible, so it must be a text message.” | Prove the exact field evaluates messages; SQL/workflow metadata may bypass substitution. |
| “Translate `complete`; keep `#TASK_ID#` unchanged.” | Preserve the entire internal row-ID link template. |
| “The token stays exact in every language.” | Substitution or escaping can still happen at the wrong layer or twice. |
| “The batch validated, the deadline is close, or review wastes prior work.” | Pressure never replaces field-level diff review; revert unsafe conversions before claiming completion. |

## Stop signs

- A key replaces `&P...`, `#COLUMN#`, `!RAW`, `!HTML`, functional markup, or spacing.
- A workflow/task metadata field receives a key without runtime evidence.
- A bulk pass lacks a candidate-to-consumer inventory or source diff review.

Stop and repair the source before validation or import.
