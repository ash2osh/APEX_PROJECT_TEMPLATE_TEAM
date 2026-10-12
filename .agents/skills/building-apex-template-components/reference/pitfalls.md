# Pitfalls: symptom, cause, fix

Each row was met while building a real component.

| Symptom | Cause | Fix |
| --- | --- | --- |
| Console: `<name> is not defined` for every card, cards have no buttons | Alpine started before the component was registered (code in the page's global declaration, or listed after Alpine) | register in a static file loaded BEFORE Alpine, via the plug-in's `javaScript fileUrls` array (`alpine-optional.md`) |
| The card collapses into one narrow row, no layout | markup uses utility classes that do not exist in the theme (`u-flex-column`) | write your own prefixed classes in your CSS file |
| Icons sit in the top-left corner of their box | the theme's `.fa` rule overrides `display` and `line-height` of the element that carries it | box `<span class="icon-box"><span class="fa fa-user"></span></span>`; style the box, not the glyph |
| A fact with an empty value still shows | `{if X%assigned/}` is true when the attribute is mapped, not when it has a value | hide in the browser (`x-show` on the text content, or `:has(.value:empty)`) |
| `#ATTRIBUTE#` renders empty | the attribute is not mapped in the region's `settings` block | add `apexlangName: COLUMN` to `settings` |
| Validator: `Invalid property: page` (or similar) in a nested block | a `//` comment inside a `{ }` property block | move the comment above the component |
| Validator: `Invalid LOV required parameter: behavior - action` | wrong keyword (`submit`) | use one of the listed values (`submitPage`) |
| Unknown region type | wrong prefix | app component: `appTemplateComponent/<apexlangName>`; theme component: `themeTemplateComponent/<name>` |
| A button stays on screen (or is missing) after an action changed its condition | `apex.region().refresh()` redraws the region's rows only; page-level button conditions are evaluated at page load | reload the page; keep scroll position and the success message in `sessionStorage` |
| The page lists nothing after an "input cleaning" change, no error | `TO_DATE(NULL)` returns NULL without raising, so a fallback in the `EXCEPTION` branch never runs | handle NULL explicitly; test with an empty value, a wrong format and a value in another format |
| `TO_DATE('13-OCT-26','YYYY-MM-DD')` gives year 13 | the format mask is lenient | use the ISO mask only for text that matches `^[0-9]{4}-[0-9]{2}-[0-9]{2}`, otherwise the session format |
| Arabic never appears although the messages exist | nothing sets the session language | add a switch that calls `APEX_UTIL.SET_SESSION_LANG` (and sets the app's own language item if queries use one) |
| Some words stay English in Arabic mode | the words come from data (reason names, package names), not from text messages | translate the data or keep a second column; text messages cannot fix data |
| `&{KEY}.` is shown literally | the field does not resolve text messages (workflow metadata, some report columns), or the key is missing | check the field type; keep that value literal or translate it in SQL with `APEX_LANG.MESSAGE` |
| Run-time `ORA-00942` in a page process that "compiles" in Builder | the process references an object that does not exist in this database | parse-compile every process and query against the parsing schema before import |
| A copied legacy process contains an outbound HTTP call, a token or a fixed phone number | the old page was copied, not read | read every line of copied PL/SQL; remove credentials and outbound calls from page code; notifications belong on the server with configuration |
| A driver can act on someone else's row by changing an id | the AJAX process trusted ids from the browser | check ownership and the parent-child relation on the server, derive the child's other ids from the database, roll back and answer with a message key on failure |
| Static file returns 404 | the file is in the folder but not registered in `static-files.apx`, or the name differs | register it with its MIME type; reference it as `#APP_FILES#name` |
| An automated browser test hangs | a native `confirm()` dialog is open | override `window.confirm` in the test, or handle the dialog through the daemon |
| The next day's list shows an old run or nothing | session state of an item (a remembered id) outlives its meaning | validate remembered ids on every render and drop one that is no longer valid |
