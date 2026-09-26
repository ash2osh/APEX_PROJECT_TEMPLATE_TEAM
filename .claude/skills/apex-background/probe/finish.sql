-- Arguments: application id. Waits for expected work, disables its schedule,
-- and exports evidence even when orchestration fails. A failed finish leaves
-- the run active so the operator can inspect the report and retry or uninstall.
SET DEFINE ON
SET SERVEROUTPUT ON
SET VERIFY OFF
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
DEFINE app_id = '&1'

DECLARE
  l_run_id NUMBER := apex_bg_probe.current_run;

  PROCEDURE wait_for(p_context VARCHAR2, p_expected PLS_INTEGER, p_seconds PLS_INTEGER) IS
    l_count PLS_INTEGER;
  BEGIN
    FOR i IN 1 .. CEIL(p_seconds / 5) LOOP
      SELECT COUNT(*) INTO l_count
        FROM apex_bg_probe_log
       WHERE run_id = l_run_id
         AND context_name = p_context
         AND probe_name = 'CONTEXT_COMPLETE'
         AND probe_value = 'OK';
      EXIT WHEN l_count >= p_expected;
      DBMS_SESSION.SLEEP(5);
    END LOOP;
    IF l_count < p_expected THEN
      raise_application_error(-20004, 'Timed out waiting for ' || p_expected
        || ' completed instance(s) of probe context ' || p_context);
    END IF;
  END wait_for;

  PROCEDURE disable_schedule IS
  BEGIN
    apex_session.create_session(p_app_id => &&app_id, p_page_id => 1, p_username => 'PROBE_USER');
    apex_automation.disable(p_application_id => &&app_id, p_static_id => 'bg-probe-scheduled');
    COMMIT;
  EXCEPTION
    WHEN OTHERS THEN
      apex_bg_probe.record_issue('DISABLE_SCHEDULE', SQLERRM);
  END disable_schedule;

  PROCEDURE wait_background_execution IS
    l_execution_id NUMBER;
    l_execution apex_background_process.t_execution;
  BEGIN
    SELECT TO_NUMBER(probe_value) INTO l_execution_id
      FROM apex_bg_probe_log
     WHERE log_id = (
       SELECT MAX(log_id)
         FROM apex_bg_probe_log
        WHERE run_id = l_run_id
          AND context_name = 'EXECUTION_CHAIN_BACKGROUND'
          AND probe_name = 'BACKGROUND_EXECUTION_ID'
          AND probe_value IS NOT NULL
     );

    FOR i IN 1 .. 36 LOOP
      l_execution := apex_background_process.get_execution(
        p_application_id => &&app_id,
        p_execution_id => l_execution_id);
      EXIT WHEN l_execution.state IN (
        apex_background_process.c_status_success,
        apex_background_process.c_status_failed,
        apex_background_process.c_status_terminated);
      DBMS_SESSION.SLEEP(5);
    END LOOP;

    IF l_execution.state <> apex_background_process.c_status_success THEN
      apex_bg_probe.record_issue(
        'BACKGROUND_EXECUTION:' || TO_CHAR(l_execution_id),
        l_execution.state || NVL2(l_execution.last_status_message, ': ' || l_execution.last_status_message, ''));
    END IF;
  EXCEPTION
    WHEN OTHERS THEN
      apex_bg_probe.record_issue('BACKGROUND_EXECUTION', SQLERRM);
  END wait_background_execution;
BEGIN
  apex_bg_probe.record_context_complete('FINISH_ATTEMPT_START');
  BEGIN
    wait_for('PAGE_PROCESS_FOREGROUND', 1, 120);
    wait_for('EXECUTION_CHAIN_BACKGROUND', 1, 180);
    wait_for('AUTOMATION_ON_DEMAND', 1, 120);
    wait_for('AUTOMATION_ON_DEMAND_BACKGROUND', 1, 120);
    wait_for('AUTOMATION_SCHEDULED', 1, 120);
    wait_for('WORKFLOW_START', 1, 120);
    wait_for('WORKFLOW_AFTER_WAIT', 1, 240);
    wait_for('TASK_ACTION_CREATE', 2, 120);
    wait_for('TASK_ACTION_COMMENT', 1, 120);
    disable_schedule;

    FOR t IN (
      SELECT t.task_id
        FROM apex_tasks t
       WHERE t.application_id = &&app_id
         AND (t.detail_pk = TO_CHAR(l_run_id)
              OR EXISTS (
                SELECT 1
                  FROM apex_workflows w
                 WHERE t.workflow_id = w.workflow_id
                   AND w.application_id = &&app_id
                   AND w.workflow_def_static_id = 'bg-probe-workflow'
                   AND w.detail_pk = TO_CHAR(l_run_id)
              ))
         AND t.state_code IN ('UNASSIGNED', 'ASSIGNED')
    ) LOOP
      BEGIN
        apex_human_task.add_task_comment(
          p_task_id => t.task_id,
          p_text => 'comment text for probe task ' || TO_CHAR(t.task_id));
      EXCEPTION
        WHEN OTHERS THEN
          apex_bg_probe.record_issue('ADD_TASK_COMMENT:' || TO_CHAR(t.task_id), SQLERRM);
      END;
      BEGIN
        apex_human_task.approve_task(p_task_id => t.task_id, p_autoclaim => TRUE);
      EXCEPTION
        WHEN OTHERS THEN
          apex_bg_probe.record_issue('APPROVE_TASK:' || TO_CHAR(t.task_id), SQLERRM);
      END;
    END LOOP;
    COMMIT;
    wait_for('TASK_ACTION_COMMENT', 3, 120);
    wait_for('TASK_ACTION_COMPLETE', 2, 120);
    wait_for('WORKFLOW_AFTER_TASK', 1, 120);
    wait_background_execution;
  EXCEPTION
    WHEN OTHERS THEN
      apex_bg_probe.record_issue('ORCHESTRATION', SQLERRM);
      disable_schedule;
      wait_background_execution;
  END;
END;
/

-- The run's tasks are now complete and its schedule is disabled. Refresh the
-- marked probe package body before the retryable completion update.
@@package-body.sql
SET DEFINE ON

SET SQLFORMAT CSV
SET FEEDBACK OFF
SPOOL log.csv
SELECT context_name, probe_name, probe_value, probe_error
  FROM apex_bg_probe_log
 WHERE run_id = apex_bg_probe.current_run
   AND context_name <> 'FINISH_ATTEMPT_START'
   AND (context_name <> 'FINISH'
        OR log_id > (
          SELECT MAX(s.log_id)
            FROM apex_bg_probe_log s
           WHERE s.run_id = apex_bg_probe.current_run
             AND s.context_name = 'FINISH_ATTEMPT_START'
             AND s.probe_name = 'CONTEXT_COMPLETE'
        ))
 ORDER BY log_id;
SPOOL OFF

SPOOL faults.csv
SELECT 'probe' AS source,
       context_name AS name,
       probe_name || ': ' || probe_error AS detail
  FROM apex_bg_probe_log l
 WHERE l.run_id = apex_bg_probe.current_run
   AND l.probe_error IS NOT NULL
   AND (l.context_name <> 'FINISH'
        OR l.log_id > (
          SELECT MAX(s.log_id)
            FROM apex_bg_probe_log s
           WHERE s.run_id = apex_bg_probe.current_run
             AND s.context_name = 'FINISH_ATTEMPT_START'
             AND s.probe_name = 'CONTEXT_COMPLETE'
        ))
UNION ALL
SELECT 'workflow activity', a.static_id,
       a.state || NVL2(a.error_message, ': ' || a.error_message, '')
  FROM apex_workflow_activities a
  JOIN apex_workflows w ON w.workflow_id = a.workflow_id
 WHERE w.application_id = &&app_id
   AND w.workflow_def_static_id = 'bg-probe-workflow'
   AND w.detail_pk = TO_CHAR(apex_bg_probe.current_run)
   AND a.state IN ('FAULTED', 'ERROR')
UNION ALL
SELECT 'automation', l.automation_static_id, l.message_type || ': ' || l.message
  FROM apex_automation_msg_log l
  JOIN apex_appl_automations m ON m.automation_id = l.automation_id
  JOIN apex_bg_probe_run r ON r.run_id = apex_bg_probe.current_run
 WHERE m.application_id = &&app_id
   AND m.static_id IN (
       'bg-probe-on-demand', 'bg-probe-on-demand-bg', 'bg-probe-scheduled')
   AND l.message_type = 'ERROR'
   AND l.message_timestamp >= r.started_at
UNION ALL
SELECT 'task', TO_CHAR(t.task_id), t.state_code
  FROM apex_tasks t
 WHERE t.application_id = &&app_id
   AND (t.detail_pk = TO_CHAR(apex_bg_probe.current_run)
        OR EXISTS (
          SELECT 1
            FROM apex_workflows w
           WHERE t.workflow_id = w.workflow_id
             AND w.application_id = &&app_id
             AND w.workflow_def_static_id = 'bg-probe-workflow'
             AND w.detail_pk = TO_CHAR(apex_bg_probe.current_run)
        ))
   AND t.state_code <> 'COMPLETED'
UNION ALL
SELECT 'finish', probe_name, probe_error
  FROM apex_bg_probe_log
 WHERE run_id = apex_bg_probe.current_run
   AND context_name = 'FINISH'
   AND log_id > (
     SELECT MAX(s.log_id)
       FROM apex_bg_probe_log s
      WHERE s.run_id = apex_bg_probe.current_run
        AND s.context_name = 'FINISH_ATTEMPT_START'
        AND s.probe_name = 'CONTEXT_COMPLETE'
   )
   AND probe_error IS NOT NULL;
SPOOL OFF

DECLARE
  l_run_id NUMBER := apex_bg_probe.current_run;
  l_missing_markers PLS_INTEGER;
  l_runtime_faults PLS_INTEGER;
BEGIN
  SELECT COUNT(*) INTO l_missing_markers
    FROM (
      SELECT 'SQLCL_NO_SESSION' context_name, 1 expected FROM dual UNION ALL
      SELECT 'SQLCL_APEX_SESSION', 1 FROM dual UNION ALL
      SELECT 'AUTOMATION_ON_DEMAND', 1 FROM dual UNION ALL
      SELECT 'AUTOMATION_ON_DEMAND_BACKGROUND', 1 FROM dual UNION ALL
      SELECT 'AUTOMATION_SCHEDULED', 1 FROM dual UNION ALL
      SELECT 'TASK_ACTION_CREATE', 2 FROM dual UNION ALL
      SELECT 'TASK_ACTION_COMPLETE', 2 FROM dual UNION ALL
      SELECT 'WORKFLOW_START', 1 FROM dual UNION ALL
      SELECT 'WORKFLOW_AFTER_WAIT', 1 FROM dual UNION ALL
      SELECT 'WORKFLOW_AFTER_TASK', 1 FROM dual UNION ALL
      SELECT 'TASK_ACTION_COMMENT', 3 FROM dual UNION ALL
      SELECT 'PAGE_PROCESS_FOREGROUND', 1 FROM dual UNION ALL
      SELECT 'EXECUTION_CHAIN_BACKGROUND', 1 FROM dual
    ) expected
   WHERE (SELECT COUNT(*)
            FROM apex_bg_probe_log l
           WHERE l.run_id = l_run_id
             AND l.context_name = expected.context_name
             AND l.probe_name = 'CONTEXT_COMPLETE'
             AND l.probe_value = 'OK') < expected.expected;

  SELECT COUNT(*) INTO l_runtime_faults FROM (
    SELECT 1
      FROM apex_bg_probe_log
     WHERE run_id = l_run_id
       AND context_name = 'FINISH'
       AND log_id > (
         SELECT MAX(s.log_id)
           FROM apex_bg_probe_log s
          WHERE s.run_id = l_run_id
            AND s.context_name = 'FINISH_ATTEMPT_START'
            AND s.probe_name = 'CONTEXT_COMPLETE'
       )
       AND probe_error IS NOT NULL
    UNION ALL
    SELECT 1
      FROM apex_workflow_activities a
      JOIN apex_workflows w ON w.workflow_id = a.workflow_id
     WHERE w.application_id = &&app_id
       AND w.workflow_def_static_id = 'bg-probe-workflow'
       AND w.detail_pk = TO_CHAR(l_run_id)
       AND a.state IN ('FAULTED', 'ERROR')
    UNION ALL
    SELECT 1
      FROM apex_automation_msg_log l
      JOIN apex_appl_automations m ON m.automation_id = l.automation_id
      JOIN apex_bg_probe_run r ON r.run_id = l_run_id
     WHERE m.application_id = &&app_id
       AND m.static_id IN ('bg-probe-on-demand', 'bg-probe-on-demand-bg', 'bg-probe-scheduled')
       AND l.message_type = 'ERROR'
       AND l.message_timestamp >= r.started_at
    UNION ALL
    SELECT 1
      FROM apex_tasks t
     WHERE t.application_id = &&app_id
       AND (t.detail_pk = TO_CHAR(l_run_id)
            OR EXISTS (
              SELECT 1
                FROM apex_workflows w
               WHERE t.workflow_id = w.workflow_id
                 AND w.application_id = &&app_id
                 AND w.workflow_def_static_id = 'bg-probe-workflow'
                 AND w.detail_pk = TO_CHAR(l_run_id)
            ))
       AND t.state_code <> 'COMPLETED'
  );

  IF l_missing_markers > 0 OR l_runtime_faults > 0 THEN
    raise_application_error(-20005,
      'finish incomplete; evidence was exported to log.csv and faults.csv; inspect with the report phase and retry or uninstall');
  END IF;

  apex_bg_probe.complete_run;
END;
/
PROMPT APEX_BG_PROBE_FINISHED
EXIT SUCCESS COMMIT
