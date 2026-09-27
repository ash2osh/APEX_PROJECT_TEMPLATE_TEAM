# Oracle APEX APEXLANG / YAML Export Debugging Guide

This guide documents the systematic debugging process used to diagnose and resolve unhandled export failures (specifically `ORA-01403: no data found` in `APEX_260100.WWV_META_META_DATA`) when exporting an Oracle APEX application in **APEXLANG** (`.apx`) or `READABLE_YAML` format.

---

## 1. Symptom & Background

### Symptom
Running a full-application APEXLANG export via SQLcl:
```sql
apex export -applicationid <APP_ID> -exptype APEXLANG -dir scratch -debug -overwrite-files
```
fails with an unhandled exception stack:
```text
ORA-01403: no data found
ORA-06512: at "APEX_260100.WWV_FLOW_EXPORT_INT", line 2691
ORA-06512: at "APEX_260100.WWV_META_META_DATA", line 5240
ORA-06512: at "APEX_260100.WWV_META_META_DATA", line 2179
ORA-06512: at "APEX_260100.WWV_META_META_DATA", line 4505
ORA-06512: at "APEX_260100.WWV_META_META_DATA", line 5187
ORA-06512: at "APEX_260100.WWV_FLOW_EXPORT_INT", line 2634
ORA-06512: at "APEX_260100.WWV_FLOW_EXPORT_INT", line 2825
ORA-06512: at "APEX_260100.WWV_FLOW_EXPORT_API", line 110
```

### Why Plain SQL Export Works but APEXLANG Fails
- **SQL export (`APPLICATION_SOURCE`, e.g. `f120.sql`)**: Dumps raw database column values directly as PL/SQL literal strings (`p_action => 'PLUGIN_APEX.FLOATING.BUTTON.MENU'`). It does not validate or expand foreign metadata references.
- **APEXLANG / YAML export**: Translates relational metadata into human-readable APEXlang YAML notation (e.g. `action: plugin/<pluginApexlangName>`). To do this, the metadata engine queries internal dictionary tables (`WWV_FLOW_PLUGINS`, etc.) to resolve component identifiers. When a referenced plugin does not exist in the application, a `SELECT ... INTO` query in `WWV_META_META_DATA` raises `NO_DATA_FOUND` (`ORA-01403`), terminating the entire export before any files are written to disk.

---

## 2. Phase 1: Capture Verbose Error & Call Stack

Run the export in SQLcl with `-debug` enabled:
```sql
apex export -applicationid <APP_ID> -exptype APEXLANG -dir scratch -debug -overwrite-files
```

Or execute the call in a PL/SQL block with full error backtrace:
```sql
set serveroutput on
declare
    l_files apex_t_export_files;
begin
    l_files := apex_export.get_application(
        p_application_id => <APP_ID>,
        p_type           => 'APEXLANG'
    );
    dbms_output.put_line('Success: ' || l_files.count || ' files');
exception
    when others then
        dbms_output.put_line('Error: ' || sqlerrm);
        dbms_output.put_line('Backtrace:' || chr(10) || dbms_utility.format_error_backtrace);
end;
/
```

If the backtrace points to `WWV_META_META_DATA`, the failure is caused by an **unresolved/broken metadata linkage** (an orphaned reference to a plugin, template, or component).

---

## 3. Phase 2: Component Isolation (Find the Failing Page)

APEX processes components sequentially starting from Page 0 (Global Page). If Page 0 fails, the whole application export aborts immediately.

To find which page is breaking the export, test each page individually using `apex_export.get_application`:

```sql
set serveroutput on
declare
    l_files         apex_t_export_files;
    l_failed_count  number := 0;
    l_success_count number := 0;
begin
    for r in (
        select page_id
        from apex_application_pages
        where application_id = :APP_ID
        order by page_id
    ) loop
        begin
            l_files := apex_export.get_application(
                p_application_id => :APP_ID,
                p_components     => apex_t_varchar2('PAGE:' || r.page_id),
                p_type           => 'APEXLANG'
            );
            l_success_count := l_success_count + 1;
        exception
            when others then
                l_failed_count := l_failed_count + 1;
                dbms_output.put_line('PAGE ' || r.page_id || ' FAILED: ' || sqlerrm);
        end;
    end loop;
    dbms_output.put_line('Summary: ' || l_success_count || ' succeeded, ' || l_failed_count || ' failed.');
end;
/
```

> **Note**: For large applications (>50 pages), run this in batches (e.g. `OFFSET 0 ROWS FETCH NEXT 30 ROWS ONLY`) to prevent client/MCP query timeouts.

---

## 4. Phase 3: Root Cause Queries for Orphan References

Once the failing page is identified (commonly **Page 0**, because Page 0 is frequently copied between applications without copying shared dependencies), query the APEX dictionary views to locate components referencing plugins that do **not** exist in the target application (`apex_appl_plugins`).

### 1. Dynamic Action Actions (Most Common)
```sql
SELECT page_id,
       dynamic_action_name,
       action_name,
       action_code,
       action_id,
       dynamic_action_id
FROM apex_application_page_da_acts
WHERE application_id = :APP_ID
  AND action_code LIKE 'PLUGIN_%'
  AND SUBSTR(action_code, 8) NOT IN (
      SELECT name
      FROM apex_appl_plugins
      WHERE application_id = :APP_ID
  );
```
*If a row appears here (e.g. `action_code = 'PLUGIN_APEX.FLOATING.BUTTON.MENU'` while `action_name` is blank), this action is an orphan referencing a non-existent plugin.*

### 2. Page Items
```sql
SELECT page_id, item_name, display_as
FROM apex_application_page_items
WHERE application_id = :APP_ID
  AND display_as IN (SELECT display_name FROM apex_appl_plugins WHERE plugin_type = 'Item Type')
  AND display_as NOT IN (SELECT display_name FROM apex_appl_plugins WHERE application_id = :APP_ID);
```

### 3. Page Processes
```sql
SELECT page_id, process_name, process_type, process_type_code
FROM apex_application_page_proc
WHERE application_id = :APP_ID
  AND process_type_code LIKE 'PLUGIN_%'
  AND SUBSTR(process_type_code, 8) NOT IN (
      SELECT name
      FROM apex_appl_plugins
      WHERE application_id = :APP_ID
  );
```

### 4. Page Regions
```sql
SELECT page_id, region_name, source_type
FROM apex_application_page_regions
WHERE application_id = :APP_ID
  AND source_type LIKE 'PLUGIN_%'
  AND SUBSTR(source_type, 8) NOT IN (
      SELECT name
      FROM apex_appl_plugins
      WHERE application_id = :APP_ID
  );
```

### 5. Check for Duplicate Plugins (Sanity Check)
Confirm whether any plugin name is duplicated:
```sql
SELECT name, count(*)
FROM apex_appl_plugins
WHERE application_id = :APP_ID
GROUP BY name
HAVING count(*) > 1;
```

### 6. Missing / Broken Template References (Regions, Buttons, Pages, Lists, Labels)
In APEXlang, every component template (region template, button template, page template, list template, etc.) is serialized as a theme template slug (e.g. `template: @/standard`, `buttonTemplate: @/text-with-icon`). When a component references a template that does **not** exist in the application's current theme, `WWV_META_META_DATA` attempts to look up the template slug in `WWV_FLOW_TEMPLATES` and throws `ORA-01403: no data found`.

Use these queries to detect missing templates:

#### A. Region Templates
```sql
SELECT r.page_id, r.region_name, r.template
FROM apex_application_page_regions r
WHERE r.application_id = :APP_ID
  AND r.template IS NOT NULL
  AND r.template != 'No Template'
  AND r.template NOT IN (
      SELECT t.template_name
      FROM apex_application_temp_region t
      WHERE t.application_id = r.application_id
  );
```

#### B. Button Templates
```sql
SELECT b.page_id, b.button_name, b.button_template
FROM apex_application_page_buttons b
WHERE b.application_id = :APP_ID
  AND b.button_template IS NOT NULL
  AND b.button_template != 'No Template'
  AND b.button_template NOT IN (
      SELECT t.template_name
      FROM apex_application_temp_button t
      WHERE t.application_id = b.application_id
  );
```

#### C. Page Templates
```sql
SELECT p.page_id, p.page_name, p.page_template
FROM apex_application_pages p
WHERE p.application_id = :APP_ID
  AND p.page_template IS NOT NULL
  AND p.page_template NOT IN (
      SELECT t.template_name
      FROM apex_application_temp_page t
      WHERE t.application_id = p.application_id
  );
```

#### D. List Templates
```sql
SELECT r.page_id, r.region_name, r.list_template
FROM apex_application_page_regions r
WHERE r.application_id = :APP_ID
  AND r.list_template IS NOT NULL
  AND r.list_template NOT IN (
      SELECT t.template_name
      FROM apex_application_temp_list t
      WHERE t.application_id = r.application_id
  );
```

#### E. Field / Item Label Templates
```sql
SELECT i.page_id, i.item_name, i.item_label_template
FROM apex_application_page_items i
WHERE i.application_id = :APP_ID
  AND i.item_label_template IS NOT NULL
  AND i.item_label_template != 'No Template'
  AND i.item_label_template NOT IN (
      SELECT t.template_name
      FROM apex_application_temp_label t
      WHERE t.application_id = i.application_id
  );
```

---

## 5. Phase 4: Remediation Methods

Once the orphan reference is identified, choose one of the following remediation paths:

### Method A: Surgical Fix via Page SQL Export (Fastest for CI/CD or Remote Dev)
If direct builder UI access is inconvenient:
1. Export the single broken page as SQL:
   - APEX Builder: Page Designer &rarr; Utilities &rarr; Export Page (or `apex export -applicationid <APP_ID> -expcomponents "PAGE:<PAGE_ID>"`).
   - This creates `f<APP_ID>_page_<PAGE_ID>.sql`.
2. Inspect `f<APP_ID>_page_<PAGE_ID>.sql` for the offending dynamic action / action call:
   ```sql
   wwv_flow_imp_page.create_page_da_event(
    p_id=>wwv_flow_imp.id(...)
   ,p_name=>'FloatingButton'
   ...
   );
   wwv_flow_imp_page.create_page_da_action(
    p_id=>wwv_flow_imp.id(...)
   ,p_action=>'PLUGIN_APEX.FLOATING.BUTTON.MENU'
   ...
   );
   ```
3. Remove the entire `create_page_da_event` and `create_page_da_action` blocks.
4. Import `f<APP_ID>_page_<PAGE_ID>.sql` back into the application.
   - The file begins with `wwv_flow_imp_page.remove_page(..., p_page_id => <PAGE_ID>);`, so importing it completely recreates the page without the orphan action.

### Method B: APEX Builder GUI
1. Open the target application in APEX Builder.
2. Navigate to the failing page (e.g. **Page 0**).
3. Under **Dynamic Actions**, locate the offending action/event (e.g. `FloatingButton`).
4. Delete the broken action or the entire dynamic action.
5. Save page changes.

### Method C: Install / Subscribe the Missing Plugin
If the component is actually required by the application:
1. Navigate to **Shared Components** &rarr; **Plugins**.
2. Click **Create** &rarr; **As a Copy of an Existing Plugin**.
3. Select an application in the workspace that has the plugin installed (e.g. Application 100).
4. Complete the copy/subscription.

---

## 6. Phase 5: Verification

1. Test exporting the isolated page:
```sql
apex export -applicationid <APP_ID> -expcomponents "PAGE:<PAGE_ID>" -exptype APEXLANG -dir scratch -overwrite-files
```
Verify exit code 0 and generated `.apx` file in `scratch/`.

2. Test exporting the entire application:
```sql
apex export -applicationid <APP_ID> -exptype APEXLANG -dir scratch -overwrite-files
```
Verify that the full application export completes without `ORA-01403` and all components are serialized into `scratch/`.
