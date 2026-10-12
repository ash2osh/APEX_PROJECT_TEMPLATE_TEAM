---
name: building-apex-template-components
description: Use when creating, changing, translating or debugging an Oracle APEX template component (plug-in type Template Component, available as Report) in an APEXlang app, for cards, lists or any repeated layout, including its attributes, region mapping, CSS and JavaScript files, mobile layout, Arabic or other languages, optional Alpine.js, and import.
---

# Building APEX template components

A **template component** (APEX 23.1+) is a plug-in that renders each row of a query through an HTML partial with placeholders. The query returns plain data; the component owns the markup, CSS and JavaScript; pages reuse it. This skill covers the **report** type at application level (region type `appTemplateComponent/<name>`), written as APEXlang source, tested in a browser, and imported.

Verified on APEX 26.1 (compatibility mode 24.2), `uc-apx` and SQLcl `apex validate`, with real imports and browser runs. The templates in `templates/` validate as they are.

## Choose the right tool

| You need | Use |
| --- | --- |
| A tabular list or a standard card list with built-in look | Interactive Report, Classic Report or a Cards region: no component needed |
| The same custom card or row on several pages, with its own CSS and behaviour | **Template component** (this skill) |
| A one-off block of text or HTML | Static content region |
| HTML assembled inside SQL (`'<button...'||col`) or an old classic-report template with `#COLUMN#` HTML | Do not copy it. Move the markup into a template component and make the SQL return data only |

Alpine.js is **optional**. A template component with plain HTML/CSS, or with one delegated vanilla click handler, is complete. Use Alpine only for per-card state (see `reference/alpine-optional.md`).

## Workflow

1. **Read first.** Find an existing component or region in the repo (`shared-components/plugins/template-component/`, `grep appTemplateComponent`). Read the project's rules (AGENTS.md: shared DEV, publish workflow, comment tags). If the repo has no example, get a Builder skeleton from the owner (`reference/testing-and-import.md` section 1) instead of guessing the syntax.
2. **Design the data contract.** One row per card; one column per value; one custom attribute per column the card uses (at most 25). Decide which values are identifiers or codes (safe for `data-` attributes) and which are free text (element content only). Status words and labels are translated texts, not data.
3. **Write the component** from `templates/`: `plugin.apx` (partial, body and row wrappers, lazy-loading skeleton, css and javaScript file lists), `custom-attributes.apx`, the CSS and JavaScript static files and their `static-files.apx` entries. Syntax and every placeholder: `reference/anatomy.md`.
4. **Write the region** on the page (`templates/region.apx`): `type: appTemplateComponent/<name>`, SQL returning plain data, `settings { attribute: COLUMN }` for every attribute, one `column` block per mapped column, `pagination`, `messages { whenNoDataFound }`, a stable `htmlDomId`.
5. **Translate** (next section).
6. **Make it mobile first and right-to-left safe** (`reference/mobile-and-rtl.md`): one column, 44px touch targets, collapse secondary facts on phones, logical CSS properties.
7. **Server code**: any action goes through one AJAX callback that checks ownership and relations on the server, calls the package routines the app already has, rolls back on failure and answers with a message **key** (`reference/pitfalls.md`, security rows).
8. **Validate offline**: SQLcl `apex validate` (always available) and `uc-apx validate` (optional tool, when installed), parse-compile every query and PL/SQL block against the parsing schema, run the logic with empty, wrong and odd inputs, run the project tests, add a source-rule test for each rule you rely on.
9. **Test in Chrome before asking for approval** (`reference/testing-and-import.md` section 4, and the `chrome-devtools-mcp` skill): look at the console and a screenshot, prototype fixes by injecting CSS/JS in the live page, let the owner verify, then put exactly that into source.
10. **Import** through the project's workflow; expect sessions to end; test again with no injected fixes, on a phone viewport, in both languages, with a long list and with a user who must not see someone else's rows.

## Translation (text messages)

- Texts live in `shared-components/messages.apx`, one `textMessage KEY` block per language, `usedInJavaScript: true` for any key read by JavaScript. Use the same placeholders (`%0`, `%1`) in every language.
- Static labels (page title, region name, item label, button label, LOV "null" text, `whenNoDataFound`): `"&{KEY}."` in this app (compatibility mode 24.2+). A literal `&{KEY}.` on screen means that field does not resolve messages: keep that value literal or translate it in SQL.
- Text inserted by SQL: `APEX_LANG.MESSAGE('KEY', arg0, arg1)`. Text inserted by the browser: `apex.lang.getMessage('KEY')`.
- A server answer carries a **key** (`{"success":false,"key":"ERR_X","detail":"..."}`); the browser translates it and writes `detail` only to the debug console.
- **Nothing sets the session language by itself.** If the app derives its language from the session, add a switch that calls `APEX_UTIL.SET_SESSION_LANG` (and sets any app item that other queries read), otherwise the second language can never show.
- Data words (reason names, package names) are not text messages; a translated page can still show them in one language.
- Conversion rules for existing apps (what must not be turned into a key): the `safeguarding-apexlang-text-messages` skill.

## Security and hygiene

- No credentials, tokens, fixed phone numbers or outbound HTTP calls in a partial, a page process or a static file. Read copied legacy PL/SQL line by line.
- HTML in SQL is a smell: it bypasses the template's escaping. Escape by default; print free text as element content.
- Hidden identity items (driver id, date) are value-protected and re-derived on the server for restricted roles; never trust an id sent by the browser.
- Comment every change with the project's tag and date.

## Pitfalls that cost real time

Full table in `reference/pitfalls.md`. The first ones to check when something looks wrong:

1. `X is not defined` from Alpine: load order (own script before Alpine, plug-in file list).
2. Layout collapsed: you used theme utility classes that do not exist; use your own prefixed classes.
3. Icon in the corner of its box: `fa` on the box itself.
4. Empty fact still visible: `{if X%assigned/}` tests the mapping, not the value.
5. `#ATTR#` empty: missing line in the region's `settings`.
6. Button not updating after an action: region refresh does not re-evaluate page-level conditions; reload the page.
7. "Cleaned" input made the page empty: `TO_DATE(NULL)` does not raise; test empty, wrong-format and other-format input.
8. Arabic never shows: nothing set the session language.

## Files

- `reference/anatomy.md` - every file, property and placeholder; region syntax; what a refresh does.
- `reference/alpine-optional.md` - when to use Alpine, load order, partial patterns, the vanilla alternative.
- `reference/mobile-and-rtl.md` - phone layout, long lists, Arabic.
- `reference/testing-and-import.md` - Builder skeleton, throwaway-worktree export, offline validation, import facts, browser testing and injection.
- `reference/pitfalls.md` - symptom, cause, fix.
- `templates/` - `plugin.apx`, `custom-attributes.apx`, `region.apx`, `ticket-card.css`, `ticket-card.js` (vanilla), `ticket-card-alpine.js` (optional).
