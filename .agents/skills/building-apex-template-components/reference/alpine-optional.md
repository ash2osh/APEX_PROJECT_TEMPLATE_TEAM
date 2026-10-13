# Alpine.js in a template component (optional)

A template component works without any JavaScript framework. Add Alpine.js only when a card needs its own small state: expand and collapse, a busy flag, buttons that depend on the row's status. For a read-only card, or when one delegated click handler is enough, use `../templates/ticket-card.js` and leave Alpine out.

## When Alpine pays off

- Per-card state (`open`, `loading`) without writing DOM code.
- Buttons computed from row data (`x-for` over an `actions()` list).
- Translated text inserted into server-rendered markup (`x-text="t('KEY')"`).
- Showing or hiding a part by its content (`x-show="$el.querySelector('.value').textContent.trim() !== ''"`).

## When to skip it

- A static card, or a card whose only interaction is a link.
- A project that must not depend on a CDN and does not want to store a library (then use the vanilla file).
- Pages where several other scripts already manage the same DOM.

## Loading it correctly (the part that broke first)

The Alpine CDN build starts on its own as soon as its script runs. A component registered later is not found and every expression fails (`visitCard is not defined`).

1. Put the component code in a static file that registers `document.addEventListener('alpine:init', ...)` with `Alpine.data('name', fn)`.
2. List that file BEFORE Alpine in the plug-in's `javaScript { fileUrls: [ ... ] }`:

```
javaScript {
    fileUrls: [
        #APP_FILES#ticket-card-alpine.js
        https://cdn.jsdelivr.net/npm/alpinejs@3.14.9/dist/cdn.min.js
    ]
}
```

3. Pin the version. A CDN needs outbound access from the user's browser; to remove that dependency store `alpine.min.js` as an application static file and list `#APP_FILES#alpine.min.js` instead (adding a downloaded file is a decision for the owner).
4. Do not put the component in the page's "Function and Global Variable Declaration": it runs after Alpine has started.
5. Cards added later (a region refresh) are initialised by Alpine's own mutation observer: no extra code.

## Writing the partial for Alpine

```html
<article class="tc-card" :class="{'tc-open': open}"
         x-data="ticketCard({ticketId:'#TICKET_ID#',status:'#STATUS_CODE#'})">
  <button type="button" :disabled="loading" x-show="canOpen()" @click="openTicket()">
    <span x-text="t('TC_OPEN')"></span>
  </button>
</article>
```

- Only pass identifiers and codes into `x-data` string literals. A free-text value (a name with an apostrophe) breaks the literal: print free text as ordinary element content (`<span>#TITLE#</span>`) and read it from the DOM if the component needs it.
- Keep the data a card needs in `x-data`; never put secrets in it.
- `@click`, `:class`, `x-for` are safe inside the plug-in partial; the parser treats them as text. A literal `#` still starts a placeholder.
- Alpine attributes added to already-initialised elements by a script have no effect: change the partial, not the DOM, for a permanent fix.

## Verifying

- Browser console: no `Alpine Expression Error`. `typeof window.Alpine` is `object` and the component name resolves.
- Buttons appear for a visit/row in each status; run each once against test data.
- After a region refresh the cards still work.
