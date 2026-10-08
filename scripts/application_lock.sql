-- Expanded by application_lock.py into a private driver, never invoked with
-- SQLcl substitution arguments. All text arguments become escaped SQL literals.
set define off
set verify off
set echo off
set heading off
set feedback off
set pagesize 0
set linesize 32767
set serveroutput on size unlimited
set encoding UTF-8
whenever sqlerror exit failure rollback
whenever oserror exit failure rollback
declare
  v_group number;
  v_user varchar2(255);
  v_count number;
  v_owner varchar2(255);
  v_comment varchar2(4000);
  v_locked_on varchar2(100);
  v_copy varchar2(10);
  v_release varchar2(100);
  function state_json return varchar2 is
    v_state varchar2(32767);
  begin
    select json_object(
      'app_id' value application_id, 'workspace' value workspace,
      'schema' value owner, 'developer' value v_user,
      'run_id' value @@RUN_ID@@,
      'locked_on' value to_char(cast(locked_on as timestamp), 'YYYY-MM-DD"T"HH24:MI:SS.FF6'),
      'comment' value lock_comment returning varchar2(32767)) into v_state
    from apex_applications where application_id=@@APP_ID@@ and locked_by is not null;
    return v_state;
  exception when no_data_found then return 'null';
  end;
begin
  if sys_context('USERENV','SESSION_USER') <> @@EXPECTED_USER@@ then
    raise_application_error(-20060, 'Unexpected SQLcl session user');
  end if;
  if regexp_like(regexp_replace(sys_context('USERENV','DB_NAME') || ' ' ||
       sys_context('USERENV','DB_UNIQUE_NAME') || ' ' || sys_context('USERENV','SERVICE_NAME'),
       '(pre|non)[-_.]?(prod|prd)', ' ', 1, 0, 'i'),
       '(^|[^[:alnum:]])(production|live)[[:digit:]]*([^[:alnum:]]|$)|(prod|prd)[[:digit:]]*([^[:alnum:]]|$)|(^|[^[:alnum:]])(prod|prd)(db|[[:digit:]])', 'i') then
    raise_application_error(-20061, 'Application locks are restricted to DEV/test targets');
  end if;
  select version_no into v_release from apex_release;
  if v_release is null or not regexp_like(v_release, '^26[.]2([.][[:digit:]]+)*$') then
    raise_application_error(-20062, 'Application locks require APEX 26.2');
  end if;
  v_group := apex_util.find_security_group_id(@@WORKSPACE@@);
  if v_group is null then raise_application_error(-20063, 'Workspace not found'); end if;
  apex_util.set_security_group_id(v_group);
  select count(*), max(user_name) into v_count, v_user from apex_workspace_apex_users
   where workspace_name=@@WORKSPACE@@ and upper(user_name)=upper(@@DEVELOPER@@)
     and (is_admin='Yes' or is_application_developer='Yes');
  if v_count<>1 then
    raise_application_error(-20064, 'APEX_WORKSPACE_USERNAME must name an existing workspace developer/admin');
  end if;
  @@BODY@@
end;
/
prompt APEX_LOCK_VERIFIED:@@PHASE@@
exit success rollback
