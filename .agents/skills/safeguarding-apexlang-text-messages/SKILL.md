---
name: safeguarding-apexlang-text-messages
description: Use when localizing, bulk converting, reviewing, or repairing APEXlang `textMessages`, especially with APEX substitutions, row tokens, workflow/task metadata, HTML controls, or JavaScript mount points.
---

# Safeguarding APEXlang text messages

Classify a value by its runtime consumer and payload, not by whether users can see it. A message key is safe only when the exact field and rendering path evaluate application text messages.

## Inspect before converting

Read `application.apx`, the target components, `messages.apx`, and the consuming SQL, PL/SQL, JavaScript, or view. Record each candidate's field, consumer, substitutions, and evidence that the field resolves text messages. If that consumer is unknown, preserve the complete value and report it as unverified.

| Candidate | Default decision |
|---|---|
| Static title, region, button label, or heading in a verified message-aware field | Eligible for localization using syntax valid for this app. |
| Mixed copy such as `Alert - &APP_TITLE.` | Preserve the complete value until the field's substitution order and message behavior are verified. Do not extract only the visible words. |
| Workflow variable/parameter labels, task-definition names/parameters, or other metadata stored for later SQL/report display | Preserve stable identifiers and literal values unless the exact display consumer is proven to evaluate text messages. |
| Dynamic process success/error message containing `&ITEM.`, a bind, or an escaping directive such as `!HTML` | Preserve the entire property. Do not translate only its prose while leaving a token behind; first prove how the caller, message engine, and output escaping interact. |
| Message value with `%name` or `%0` | Localize only after verifying the caller supplies every argument; preserve placeholder names and positional counts in each locale. |
| Row link containing `#COLUMN#`, for example `complete #TASK_ID#`, or report value such as `#ACTIONS#` | Preserve the whole data-driven template. Do not translate only its verb while retaining the row token. |
| Interactive HTML, `#select!RAW#`, SQL/PLSQL/JS/CSS, route or handler, `#APP_FILES#`, or DOM mount such as `<div id="active_facets"></div>` | Preserve executable structure, identifiers, and handlers. In verified `View Sample` HTML, translate only a separate text node after checking substitutions and escaping. |
| `&nbsp;` or empty content | Preserve `&nbsp;` exactly, including its semicolon; preserve empty content. |

## Evidence-based safeguards from App 115

The App 115 APEXlang review found these failure patterns in its `textMessages` conversion. Use them as checks against the target app's consumers, not as a claim that every APEX release or field behaves identically:

- Workflow variable and task-definition parameter labels/names were persisted as metadata and later displayed by runtime reports and task pages through SQL results. Those consumers showed literal `&{KEY}.` values. Keep such metadata literal unless the actual display path is shown to resolve the key.
- Dynamic process messages containing session items or binds risked double substitution and escaping differences when moved into parameterized messages. Keep the complete dynamic message literal until the consumer and placeholder contract are verified. After bulk edits, check that each process has only one property of each kind, especially `successMessage`.
- Select-all header markup and `#select!RAW#` are interactive controls/expressions, not copy. Keep their source intact.
- Keep spacing entities as `&nbsp;`, including the semicolon; do not replace them with message keys or drop punctuation.
- Keep row link text such as `complete #TASK_ID#` intact until row-token handling in that exact link consumer is proven.
- Keep empty plugin containers and mount-point markup, including element IDs, intact.

If a mixed or data-bearing field appears on screen, visibility alone is not proof that the APEX text-message engine evaluates the field. Inspect and test the exact consumer before considering conversion.

### Bulk-conversion checklist from the App 115 review

Before editing, inventory each candidate by source file, property, original value, runtime consumer, and substitution/placeholder shape. Restrict conversion to static UI text in fields verified to resolve APEX text messages. For the App 115 failure classes above, preserve workflow/task metadata, complete dynamic process messages, checkbox markup and `#select!RAW#`, literal `&nbsp;`, row link text such as `complete #TASK_ID#`, and JavaScript mount-point HTML with its IDs.

After editing, compare the property inventory with the baseline. Check for duplicate properties (especially `successMessage`), changed identifiers, missing or orphaned keys, changed row tokens, and placeholder names/counts that differ by locale. Use `git diff --stat` as a review aid, then inspect the complete diff; insertion/deletion counts are evidence to explain, not a correctness test by themselves.

## Safe conversion and validation

1. Convert only verified static copy. Check the app's compatibility mode and loaded APEXlang workflow first. Oracle APEX 26.1 marks `&APP_TEXT$KEY.` as legacy and recommends `&{KEY}.` for compatibility mode 24.2 or later. For an older mode or a workflow that appears to require legacy syntax, verify the supported form against that app's documentation and tooling; do not choose legacy syntax from memory or switch silently.
2. Review the complete diff. Check duplicate properties, changed identifiers, unresolved or orphan message keys, and identical placeholder names/counts in every locale. For automated edits, compare the property inventory before and after.
3. Run validators that are installed and configured for the project. `uc-apx` may be run as:

   ```bash
   uc-apx validate --app-dir <app-source-dir> --json-pretty
   ```

   Require zero errors in the modified application source; report warnings separately.

   SQLcl APEXlang validation can run without a database connection:

   ```bash
   sql /nolog <<'SQL'
   apex validate -input <app-source-dir>
   exit
   SQL
   ```

   Confirm the SQLcl command reports `Validation successful.` and exits 0; this is source validation, not a live runtime translation check.

   Run `graphify update .` only when this repository has Graphify configured, to refresh its local AST graph. Finish with `git diff --check`, `git diff --stat`, and a full diff review. Do not claim an unavailable optional validator passed.
4. Source validation cannot prove runtime translation. Without live message setup, target-language entries, and authorization to exercise the app, report candidates only. Export, import, publish, or edit shared Builder state only when explicitly authorized under the project workflow.

## Live evidence: app 150 (`workflow-showcase`)

Read-only runtime check on 2026-09-27, APEX 26.1.4, compatibility mode 26.1. The app uses Text Messages and derives locale from session state. SQLcl found 142 Arabic/English message-key pairs with identical text; this does not prove every key is rendered by a page component.

- Page 1 switched between English and Arabic; Arabic reported `lang=ar` and `dir=rtl`, while its title/heading remained `workflow-showcase`.
- Page 6 reported Arabic `lang=ar` and `dir=rtl`, then English `lang=en`. Its title/heading remained `MY TASKS` in both modes.
- A page 6 task card displayed the literal `&{REQUESTAPPROVAL}.` in both locales, concrete runtime evidence that this task-card consumer did not evaluate that value as an application text message.
- The browser console had no messages during this check. No app source, Builder state, or database was changed.

The check confirms locale switching and RTL direction for the inspected pages, while the task-card key remains unevaluated and the inspected headings did not change. It does not establish behavior for uninspected components or all 142 message pairs. To fix the task-card value, inspect its task-definition and display consumer and coordinate any shared Builder/import change under the team workflow.

## Other runtime check: Universal Theme reference app 102

App 102 (`docker-demo`, APEX 26.1.4, compatibility 26.1) has no text messages or translation rows; translated output is unverified. Hold `Alert - &APP_TITLE.` pending consumer proof. `View Sample` in static HTML is a candidate only after a field check; preserve `&APP_ID.`, `&APP_SESSION.`, markup, and handlers. `#ACTIONS#` and `#ID#` are data.

## Stop signs

- A dynamic process message or row link is partially translated while its runtime token is retained.
- A workflow/task metadata value is keyed without evidence from its actual display consumer.
- A bulk replacement changes a property more than once, changes an identifier, or lacks an inventory and full diff review.
- A visible string is called static without checking substitutions, escaping, and consumer behavior.

Stop, restore the functional/data-bearing source, and reclassify the field before validation or any authorized import.
