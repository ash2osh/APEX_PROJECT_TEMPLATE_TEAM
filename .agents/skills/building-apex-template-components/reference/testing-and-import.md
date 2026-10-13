# Testing, importing and verifying a template component

## 1. Get the exact syntax first when the repo has no example

1. Ask the owner (a person with Builder access) to create a **skeleton** in App Builder and tell you when it is saved:
   - Shared Components > create a Template Component (plug-in type "Template Component"), name `<NAME>`, Available As **Report**, a partial such as `<div class="x">#ONE# #TWO#</div>`, two custom attributes.
   - A scratch page (for example page 9990) with a region that uses the component and maps both attributes to the columns of `select 1 one, 'a' two from dual`.
2. Export in a **throwaway git worktree**, so your working tree and unpublished edits are not overwritten by the export:

```bash
git worktree add --detach /tmp/export-wt HEAD
(cd /tmp/export-wt && scripts/team.sh export <app id>)
git -C /tmp/export-wt status --short | grep -v '^ M'      # the new plug-in folder and the scratch page
```

3. Copy only the new files (`shared-components/plugins/template-component/<name>/`, the scratch page for reading) into your tree. Read the region block: that is the syntax to follow. Keep `apex-team-export.json` of that export as the new publish baseline (see section 4). Remove the worktree (`git worktree remove --force`, `git worktree prune`). Delete the scratch page later: it disappears with the next import that does not contain it.

## 2. Validate offline before any import

```bash
uc-apx validate --app-dir apps/<SCHEMA>/<app id>                      # optional tool; errors must be 0
printf 'apex validate -input apps/<SCHEMA>/<app id>\nexit\n' | sql -nolog   # "Validation successful."
python3 -m unittest discover -s tests                                  # the project's source rules
```

SQLcl names the invalid property and lists the allowed values. Run both validators when `uc-apx` is installed: each catches things the other does not.

Compile-check the SQL and PL/SQL of the page without running it:

```sql
declare c integer := dbms_sql.open_cursor;
begin dbms_sql.parse(c, q'~<paste the query or the begin..end block>~', dbms_sql.native);
      dbms_output.put_line('PARSE OK');
exception when others then dbms_output.put_line('PARSE FAILED ' || sqlerrm); end;
/
```

Connect as the **parsing schema** of the app (the one the page runs as). A block that references a missing object fails here, as a page process would at run time. Parsing proves it compiles, not that its logic is right: run the logic with real inputs too (an empty value, a wrong format, a value of another type).

Write a source-rule test for each lesson (a regex over the page, the plug-in and the static files) so it cannot silently come back.

## 3. Import

Follow the project's publish workflow (shared DEV: tell the team first). Facts that matter for a template component:

- The import replaces the whole app; objects that exist only in the database (the Builder skeleton, the scratch page) are removed.
- It ends every signed-in session (the owner has to log in again) and disables the app's automations: use the project's wrapper that restores them.
- Page-catalogue and grant tables used by the app's own menu are data, not app source: apply their migration before the import.
- The post-import byte comparison can report formatting differences (comments dropped, defaults elided); that is cosmetic, check the diff, do not force.

## 4. Verify in Chrome (with the project's `chrome-devtools-mcp` skill)

Work in a duplicate tab (`window.open(location.href)` from the owner's tab keeps the session id inside the browser); never write a session id or URL to a file.

1. **Look at the real state first**: `list_console_messages` (no `Alpine Expression Error`, no 404 for your static files), `take_screenshot`, and `evaluate_script` returning counts and text.
2. **Prototype a fix in the browser, not in the source**: inject a `<style id="hotfix">` with the CSS and patch functions on `window`; reload loses it, so re-inject after each reload. Let the owner look at the live result and approve before you edit source. Then put exactly the verified change into source, import, and test again without any injection.
3. **Emulate a phone**: `emulate` with `{"viewport":"390x844x2,mobile,touch"}` (reload the tab afterwards). Take a full-page screenshot (`fullPage: true`) for long lists.
4. **Drive the real actions**: override `window.confirm = () => true` for the test, click the buttons by their text, wait, then read the database (read-only SQL) to confirm the state really changed. Test each status transition, a failure path (empty reason, a forbidden id) and a long list.
5. **Test both languages and both directions** on the phone viewport.
6. **Test ownership and validation through the AJAX process**: call `apex.server.process(...)` with ids that are not yours and check the answer and that the database did not change.
7. Put sample data back afterwards (the package routines the app itself uses, not hand-written updates).

Screenshots through the daemon come back as inline base64 in the JSON; save the image with a short script and read it. Do not call `take_screenshot` on a background tab (it hangs); bring the tab to the front first.
