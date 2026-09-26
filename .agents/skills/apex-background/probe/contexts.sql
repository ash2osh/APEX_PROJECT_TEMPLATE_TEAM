-- Arguments: application id, run label. Starts a run and triggers SQLcl contexts.
SET DEFINE ON
SET SERVEROUTPUT ON
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
DEFINE app_id = '&1'
DEFINE run_label = '&2'

DECLARE
  l_run_id NUMBER;
  l_task_id NUMBER;
  l_workflow_id NUMBER;
  l_schedule_enabled BOOLEAN := FALSE;
BEGIN
  BEGIN
    apex_bg_probe.start_run('&&run_label');
    l_run_id := apex_bg_probe.current_run;
    DBMS_OUTPUT.PUT_LINE('probe run ' || l_run_id);
    apex_bg_probe.capture('SQLCL_NO_SESSION');
    apex_bg_probe.record_context_complete('SQLCL_NO_SESSION');

    apex_session.create_session(p_app_id => &&app_id, p_page_id => 1, p_username => 'PROBE_USER');
    apex_util.set_session_state('PROBE_APP_ITEM', 'app-item-from-sqlcl');
    apex_util.set_session_state('P1_PROBE_ITEM', 'page-item-from-sqlcl');
    apex_bg_probe.capture('SQLCL_APEX_SESSION');
    apex_bg_probe.record_context_complete('SQLCL_APEX_SESSION');

    apex_automation.execute(
      p_application_id => &&app_id,
      p_static_id => 'bg-probe-on-demand',
      p_run_in_background => FALSE);
    apex_automation.execute(
      p_application_id => &&app_id,
      p_static_id => 'bg-probe-on-demand-bg',
      p_run_in_background => TRUE);
    l_schedule_enabled := TRUE;
    apex_automation.enable(p_application_id => &&app_id, p_static_id => 'bg-probe-scheduled');

    l_task_id := apex_human_task.create_task(
      p_application_id => &&app_id,
      p_task_def_static_id => 'bg-probe-task',
      p_subject => 'BG probe standalone',
      p_initiator => 'PROBE_USER',
      p_initiator_can_complete => TRUE,
      p_parameters => apex_human_task.t_task_parameters(
        1 => apex_human_task.t_task_parameter(
          static_id => 'T_PROBE_PARAM',
          string_value => 'from-sqlcl-task')),
      p_detail_pk => TO_CHAR(l_run_id));
    DBMS_OUTPUT.PUT_LINE('standalone task ' || l_task_id);
    apex_human_task.add_task_comment(
      p_task_id => l_task_id,
      p_text => 'comment text from SQLcl task probe');

    l_workflow_id := apex_workflow.start_workflow(
      p_application_id => &&app_id,
      p_static_id => 'bg-probe-workflow',
      p_parameters => apex_workflow.t_workflow_parameters(
        1 => apex_workflow.t_workflow_parameter(
          static_id => 'P_PROBE_PARAM',
          value => apex_session_state.t_value(
            data_type => apex_session_state.c_data_type_varchar2,
            varchar2_value => 'from-sqlcl-workflow'))),
      p_initiator => 'PROBE_USER',
      p_detail_pk => TO_CHAR(l_run_id));
    DBMS_OUTPUT.PUT_LINE('workflow ' || l_workflow_id);
    COMMIT;
  EXCEPTION
    WHEN OTHERS THEN
      IF l_schedule_enabled THEN
        BEGIN
          apex_automation.disable(p_application_id => &&app_id, p_static_id => 'bg-probe-scheduled');
          COMMIT;
        EXCEPTION
          WHEN OTHERS THEN
            NULL;
        END;
      END IF;
      RAISE;
  END;
END;
/
PROMPT APEX_BG_PROBE_STARTED
EXIT SUCCESS COMMIT
