---
name: safeguarding-apexlang-text-messages
description: Use when localizing, bulk converting, reviewing, or repairing APEXlang `textMessages`, especially with APEX substitutions, row tokens, workflow/task metadata, HTML controls, or JavaScript mount points.
---

# Safeguarding APEXlang text messages

Classify fields by runtime consumer and payload, not visibility: keys are safe only where the field evaluates messages.

Use `apexlang` for canonical syntax; apply these safeguards to runtime data and executable markup.

## Classify before converting

Read `application.apx`, target components, `messages.apx`, and consuming SQL/code or views. Record fields, consumers, and substitutions.

| Candidate | Decision |
|---|---|
| Static title, region, button label, or heading in a verified message-aware field | Eligible using syntax valid for this app and workflow. |
| Mixed copy such as `Alert - &APP_TITLE.` | Hold until the consumer and substitution/parameter behavior are proven; same token text in each locale is not proof. Translate a separable text node only if semantics stay intact. |
| Workflow/task metadata or messages with `&ITEM.`, `!HTML`, `!RAW`, binds, or `%n` | Keep literal unless docs and runtime evidence prove the exact consumer evaluates messages and passes arguments safely. |
| Message value with `%name` or `%0` | Translate only if the caller supplies values; preserve named placeholders and positional counts in every locale. |
| Row link with `#COLUMN#` or report value `#ACTIONS#` | Preserve the whole data-driven template; do not translate just its verb. |
| Functional source: HTML, `#select!RAW#`, JS hooks, DOM mounts, SQL/PLSQL/JS/CSS, `#APP_FILES#`, plugin source | Preserve structure, routes, and handlers. In `View Sample` HTML with `&APP_ID.` / `&APP_SESSION.`, translate visible text only after verifying the field. |
| `&nbsp;` or empty content | Keep `&nbsp;` with semicolon; preserve empty content. |

## Safe conversion

1. Check compatibility mode and loaded APEXlang workflow. Oracle APEX 26.1 marks `&APP_TEXT$KEY.` legacy and recommends `&{KEY}.` for compatibility 24.2+. If workflow still requires `APP_TEXT$`, resolve the conflict; do not call `&{KEY}.` historical or switch silently.
2. Convert verified static copy only. Review diffs, preserve exclusions, compare placeholders, check references, and remove orphan keys.
3. Without live message setup or target-language entries, report candidates only; source review cannot prove translated output. Leave unclear consumers unchanged. Export/import only if authorized.

## Live check: Universal Theme reference app 102

App 102 (`docker-demo`, APEX 26.1.4, compatibility 26.1) has no text messages or translation rows; output is unverified. Hold `Alert - &APP_TITLE.` pending consumer proof. `View Sample` in static HTML is a candidate only after a field check; preserve `&APP_ID.`, `&APP_SESSION.`, markup, and handlers. `#ACTIONS#` / `#ID#` are data.

Reject visibility, exact-token, or deadline arguments: verify consumers, preserve full row-ID links, and review field diffs.

## Stop signs

- Mixed content is called static without checking substitutions; keys replace `&P...`, `#COLUMN#`, `!RAW`, `!HTML`, markup, or spacing.
- Workflow/task metadata is keyed without runtime evidence; bulk conversion lacks an inventory or diff review.

Stop and repair the source before validation or import.
