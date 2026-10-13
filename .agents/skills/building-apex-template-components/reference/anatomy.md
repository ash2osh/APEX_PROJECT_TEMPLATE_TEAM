# Anatomy of a report template component in APEXlang

Everything below was verified on APEX 26.1 (compatibility mode 24.2) with `uc-apx validate`, SQLcl `apex validate`, an import, and a browser run.
The files in `../templates/` are a complete, validated example (a "ticket card").

## 1. Where the files live

| Piece | Path under `apps/<SCHEMA>/<app id>/` |
| --- | --- |
| The component | `shared-components/plugins/template-component/<apexlangName>/plugin.apx` |
| Its attributes | `shared-components/plugins/template-component/<apexlangName>/custom-attributes.apx` |
| CSS and JavaScript | `shared-components/static-files/<file>` plus an entry in `shared-components/static-files.apx` (`file <name> ( mimeType: text/css  charSet: utf-8 )`; JavaScript: `text/javascript`) |
| Texts | `shared-components/messages.apx` (`textMessage KEY ( message { text language usedInJavaScript } )`, one block per language) |
| The page that uses it | `pages/pNNNNN-<alias>.apx`, a region with `type: appTemplateComponent/<apexlangName>` |

Application-level component (this guide): region type `appTemplateComponent/<apexlangName>`, `staticId: NAME`.
Theme-specific component: the plug-in has `theme: @universal-theme`, `staticId: THEME_42$NAME`, and the region type is `themeTemplateComponent/<apexlangName>` (the built-in Content Row, Media List, Timeline and Comments are of this kind). Prefer application-level: it is independent of the theme.

## 2. plugin.apx

```
plugin ticketCard (                       // apexlangName: camelCase, also the folder name and the region type suffix
    name: TICKET_CARD                     // UPPER_SNAKE display name
    staticId: TICKET_CARD
    templateComponent {
        availableAs: report               // report = one partial repeated per row
        partial: ```html ... ```          // the markup of ONE row (see section 4)
        reportBody: ```html <div class="tc-list">#APEX$ROWS#</div> ```
        reportRow:  ```html <div class="tc-list__row" #APEX$ROW_IDENTIFICATION#>#APEX$PARTIAL#</div> ```
        numberOfLazyLoadingSkeletons: 1   // rows drawn while the region loads or refreshes
    }
    component { standardAttributes: regionTemplateAttr }
    css        { fileUrls: #APP_FILES#ticket-card.css }
    javaScript { fileUrls: #APP_FILES#ticket-card.js }          // an array form [ a  b ] keeps the order, see alpine-optional.md
    information { version: 1.0 }
)
```

Short single-line values can be written inline (`reportBody: <ul>#APEX$ROWS#</ul>`); multi-line markup uses a fenced block.
A comment (`//`) is allowed between components but NOT inside a `{ }` property block: the parser reports "Invalid property".

## 3. custom-attributes.apx

One `customAttribute` per placeholder used in the partial:

```
customAttribute STATUS_CODE (
    apexlangName: statusCode      // camelCase: the key used in the region's settings block
    attribute: 3                  // unique number, 1..25
    name: Status Code
    type: sessionStateValue
    appearance { sequence: 30 }
)
```

Declare every placeholder (`#STATUS_CODE#` needs `customAttribute STATUS_CODE`). In Builder the attributes are created from the placeholders when the plug-in is saved.
At most 25 custom attributes: put several values into one column and split them in the template only if you must; usually a smaller card is better.

## 4. The partial

| Syntax | Meaning |
| --- | --- |
| `#ATTRIBUTE#` | the mapped value, HTML-escaped by default |
| `#APEX$ROWS#` | (reportBody) all rendered rows |
| `#APEX$PARTIAL#` | (reportRow) the partial of this row |
| `#APEX$ROW_IDENTIFICATION#` | (reportRow) attributes APEX needs on the row element |
| `{if APEX$IS_LAZY_LOADING/} ... {else/} ... {endif/}` | skeleton while loading, real card afterwards |
| `{if ATTRIBUTE%assigned/} ... {endif/}` | true when the attribute is MAPPED in the region, **not** when its value is non-empty: a mapped column that is NULL still counts as assigned |
| `{if APEX$HAS_ACTION_BUTTONS/} #ACTIONS# {endif/}` | region actions defined on the page (keeps the component reusable) |
| `&{KEY}.` | a text message in the session language (works in the partial in this app; see the translation section of SKILL.md) |

Rules for the markup:

- No literal `#` that is not a placeholder (a colour in an inline style, `href="#"`): `#` starts a placeholder. Put colours in the CSS file.
- Give the card's root a stable class and put interactive data in `data-` attributes (`data-ticket-id="#TICKET_ID#"`), or in an `x-data` object if you use Alpine.
- To hide a part when a value is empty, test the value in the browser (Alpine `x-show` on the text content, or CSS `.fact:has(.value:empty){display:none}`), not with `%assigned`.
- Put the icon glyph in an inner `<span class="fa fa-user">`; never put `fa` on the box that you size and centre (the theme's `.fa` rule sets `display` and `line-height`).
- Do not rely on utility classes you have not seen in the theme's CSS (`u-flex-column` and friends may not exist); write your own `mv-`/`tc-` prefixed classes.

## 5. The region on a page

```
region ticket-cards (
    name: "&{TC_REGION_TICKETS}."
    type: appTemplateComponent/ticketCard
    source { location: localDatabase  type: sqlQuery  sqlQuery: ```sql ... ``` }
    layout { sequence: 10  slot: body }
    appearance { template: @/blank-with-attributes-no-grid  templateOptions: #DEFAULT# }
    accessibility { landmarkType: region }
    advanced { htmlDomId: ticket-cards }          // a stable id: apex.region('ticket-cards') and CSS hooks
    componentAppearance { display: report }
    settings { ticketId: TICKET_ID   title: TITLE   statusCode: STATUS_CODE }   // apexlangName: COLUMN (plain column name for sessionStateValue)
    pagination { entitiesPerPage: 50 }
    messages { whenNoDataFound: "&{TC_EMPTY}."  noDataFoundIcon: fa-clipboard fa-lg }
    column TICKET_ID ( layout { sequence: 10 } source { databaseColumn: TICKET_ID  dataType: number  primaryKey: true } )
    ...one column block per mapped column
)
```

- `settings` maps each attribute to a column. Built-in theme components also accept text with substitutions (`title: &TASK_NAME.`); for a custom `sessionStateValue` attribute the export writes the plain column name.
- Every mapped column needs a `column` block; the SQL must return exactly those aliases.
- Keep SQL to plain data. No HTML, no inline styles, no `'<button...'` strings: that is what the template is for. Translated words come from `APEX_LANG.MESSAGE('KEY')` in SQL or from the browser.
- `htmlDomId` is also the id used in `apex.region('id').refresh()`.

## 6. What a region refresh does and does not do

`apex.region('id').refresh()` re-runs the region query and redraws its rows. It does **not** re-evaluate server-side conditions of buttons or other regions on the page (a "Start trip" button whose condition is a SQL `rowsReturned` stays as rendered). After an action that changes such a condition, reload the page (`window.location.reload()`, keep the scroll position and a success message in `sessionStorage`) or hide/show the element in the browser.

## 7. Syntax you cannot guess

If no example of the construct exists in the repo, do not invent it. Ask the owner to create a minimal skeleton in App Builder (see `testing-and-import.md`), export it in a throwaway worktree and copy the new files. `uc-apx shape region --list-types` and `uc-apx shape region --type <type> --json-pretty` print real examples of the built-in kinds; `sql -nolog` + `apex validate -input <dir>` names the invalid property and the allowed values (for example a button's `behavior { action: }` must be `submitPage`, not `submit`).
