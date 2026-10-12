# Mobile first, and right-to-left

Design the card for a phone (about 390px wide) first, then widen. A long list is the real test: 15 stacked cards of seven facts made a 9,200px page; collapsing each card to its key facts made it 2,100px.

## Layout

- One column by default; `repeat(2, minmax(0, 1fr))` from 600px; three or four columns from 1000-1100px. Always `minmax(0, 1fr)` so long text cannot stretch a column.
- `min-width: 0` on grid or flex children that hold text; `overflow-wrap: anywhere` on values.
- Touch targets at least 44px high; primary actions can be full width on a phone (`flex: 1 1 140px`, then `flex: 0 1 auto` from 600px).
- Keep `padding` and `gap` small on a phone (12px), larger on a desktop.
- Collapse secondary facts on a phone: a toggle, a one-line summary (name and time) and the actions stay visible; the full fact grid opens on demand. Hide the toggle and show everything on wider screens, and do it in CSS (`@media (max-width: 767px)`) so the desktop does not depend on JavaScript.
- Sticky headers and fixed bars are a trap on phones (the browser's own bars): avoid unless needed.

## Right-to-left (Arabic)

- Use logical properties: `margin-inline-start`, `padding-inline`, `inset-inline-end`, `text-align: start`. Never `margin-left`.
- Flexbox and grid flip by themselves under `dir="rtl"`; do not add `row-reverse` to "fix" it.
- Icons that indicate direction (chevrons, arrows) must be mirrored or chosen per direction; a rotating chevron (`transform: rotate(180deg)` when open) is direction-neutral.
- Numbers stay left-to-right inside Arabic text; do not wrap them in extra spans.
- The page direction is set by the APEX session language (`html[dir=rtl]` when the session language is `ar`). Test both languages on the phone-sized viewport.

## Colour and states

- Put all colours in CSS custom properties on `:root` (`--tc-accent`); status chips use a background, a border and a text colour of the same hue.
- Provide a loading skeleton that matches the card height to avoid layout jumps.
- Do not rely on colour alone for a status: the chip also has text.

## Checklist

- [ ] 390px wide: no horizontal scroll, text wraps, buttons reachable with a thumb.
- [ ] 15+ rows: the page is scannable (collapsed cards, finished rows at the bottom).
- [ ] Arabic: layout mirrors, nothing overlaps, all labels translated.
- [ ] 1480px wide: nothing stretches oddly; the grid fills the width.
