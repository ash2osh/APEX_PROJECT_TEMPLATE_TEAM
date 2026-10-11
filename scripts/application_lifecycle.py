#!/usr/bin/env python3
"""Observe APEX 26.2 runtime state and restore captured active automations."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

if __package__ in (None, ''):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.db_targets import Target, looks_like_production_identity, ORACLE_IDENTIFIER, SQLCL_ALIAS
from scripts.sqlcl_session import run_sqlcl, SqlclError

WORKFLOW_TERMINAL = {'COMPLETED', 'TERMINATED', 'FAULTED'}
TASK_TERMINAL = {'COMPLETED', 'CANCELED', 'ERRORED', 'EXPIRED', 'FAILED'}
STATES = {'workflows': WORKFLOW_TERMINAL | {'ACTIVE','SUSPENDED'}, 'tasks': TASK_TERMINAL | {'UNASSIGNED','ASSIGNED','INFO_REQUESTED'}}
ERRORS = {'workflows': {'FAULTED'}, 'tasks': {'ERRORED','FAILED'}}
IDENTITY = ('session_user','current_schema','db_unique_name','container_id','container_name','edition','server_host','service_name')
COVERAGE = ('application','workspace','automations','workflows','tasks')
MAX_BYTES = 32 * 1024 * 1024


def _positive(value):
    return isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value, re.ASCII) is not None


def validate_snapshot(data: Mapping) -> None:
    if not isinstance(data, Mapping) or type(data.get('schemaVersion')) is not int or data['schemaVersion'] != 1:
        raise ValueError('unsupported lifecycle snapshot')
    if not isinstance(data.get('release'), str) or not re.fullmatch(r'26\.2(?:\.[0-9]+)*',data['release'],re.ASCII):
        raise ValueError('lifecycle observation requires APEX 26.2')
    identity=data.get('identity')
    if not isinstance(identity,Mapping) or any(not isinstance(identity.get(key),str) or not identity[key].strip() for key in IDENTITY):
        raise ValueError('lifecycle identity is incomplete')
    if type(data.get('appId')) is not int or not 0 < data['appId'] < 10**18 or not _positive(data.get('workspaceId')) or not isinstance(data.get('workspace'),str) or not data['workspace']:
        raise ValueError('lifecycle application/workspace scope is invalid')
    if not isinstance(data.get('coverage'),Mapping) or any(data['coverage'].get(key) is not True for key in COVERAGE):
        raise ValueError('lifecycle visibility coverage is incomplete')
    try:
        times=[datetime.fromisoformat(data[key].replace('Z','+00:00')) for key in ('startedAt','completedAt')]
        if any(value.tzinfo is None for value in times) or times[0]>times[1]: raise ValueError()
    except (KeyError,TypeError,AttributeError,ValueError) as exc:
        raise ValueError('lifecycle capture timestamps are invalid') from exc
    if type(data.get('applicationPresent')) is not bool:
        raise ValueError('invalid lifecycle application visibility')
    if not data['applicationPresent'] and any(data.get(kind) for kind in ('automations','workflows','tasks')):
        raise ValueError('absent application has lifecycle rows')
    for kind in ('automations','workflows','tasks'):
        rows=data.get(kind)
        if not isinstance(rows,list): raise ValueError('lifecycle rows are incomplete')
        seen=set()
        for row in rows:
            if not isinstance(row,dict): raise ValueError('malformed lifecycle row')
            if kind=='automations':
                if set(row) != {'staticId','status'} or not all(isinstance(row[key],str) and row[key] for key in row): raise ValueError('invalid automation state')
                key=row['staticId']
            else:
                if set(row) != {'instanceId','definitionId','staticId','state','terminal'} or not _positive(row.get('instanceId')) or not _positive(row.get('definitionId')) or not isinstance(row.get('staticId'),str) or not row['staticId'] or row.get('state') not in STATES[kind]: raise ValueError('invalid lifecycle instance')
                terminal=WORKFLOW_TERMINAL if kind=='workflows' else TASK_TERMINAL
                if type(row.get('terminal')) is not bool or row['terminal'] != (row['state'] in terminal): raise ValueError('invalid terminal classification')
                key=row['instanceId']
            if key in seen: raise ValueError('duplicate lifecycle identifier')
            seen.add(key)


def compare_lifecycle(before: Mapping, after: Mapping) -> dict:
    report={'schemaVersion':1,'status':'pass','changes':[],'reasons':[],'retrySuspension':False}
    try:
        validate_snapshot(before); validate_snapshot(after)
        for key in ('identity','release','workspace','workspaceId','appId'):
            if before[key]!=after[key]: raise ValueError('lifecycle target scope changed: '+key)
    except (ValueError,TypeError) as exc:
        report.update(status='unavailable',reasons=[str(exc)]); return report
    if not after['applicationPresent']:
        report.update(status='attention',reasons=['application is absent after import'])
        return report
    for kind in ('automations','workflows','tasks'):
        key='staticId' if kind=='automations' else 'instanceId'
        old={row[key]:row for row in before[kind]}; new={row[key]:row for row in after[kind]}
        for identifier in sorted(old.keys() | new.keys()):
            left=old.get(identifier); right=new.get(identifier)
            if left==right: continue
            change={'kind':kind,'id':identifier,'before':left,'after':right}
            report['changes'].append(change)
            reason=None
            if kind=='automations':
                if left is not None: reason='previous automation definition or status changed: '+identifier
            elif right is None and not left['terminal']: reason='previously nonterminal instance is missing: '+identifier
            elif right is not None:
                if left and (left['definitionId'],left['staticId'])!=(right['definitionId'],right['staticId']): reason='instance definition changed: '+identifier
                if right['state'] in ERRORS[kind] and (not left or left['state']!=right['state']): reason='new instance error: '+identifier
                if kind=='workflows' and right['state']=='SUSPENDED' and (not left or left['state']!='SUSPENDED'):
                    reason='new workflow suspension: '+identifier; report['retrySuspension']=True
            if reason: report['reasons'].append(reason)
    if report['reasons']: report['status']='attention'
    return report


def require_verified_lifecycle(report: Mapping) -> None:
    if not isinstance(report,Mapping) or report.get('status')!='pass':
        raise ValueError('application lifecycle verification did not pass; inspect lifecycle-report.json before recovery')


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate lifecycle JSON key')
        result[key]=value
    return result


def read_snapshot(path: Path) -> dict:
    if path.stat().st_size>MAX_BYTES: raise ValueError('lifecycle snapshot exceeds size limit')
    result=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=_pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    validate_snapshot(result)
    return result


def capture_lifecycle(target: Target, workspace: str, app_id: int, run_dir: Path, *, _runner=run_sqlcl) -> dict:
    if not ORACLE_IDENTIFIER.fullmatch(target.schema) or not ORACLE_IDENTIFIER.fullmatch(target.expected_user) or not SQLCL_ALIAS.fullmatch(target.connection) or target.environment not in ('dev','staging','prod'): raise ValueError('invalid lifecycle database target')
    if type(app_id) is not int or not 0<app_id<10**18 or not isinstance(workspace,str) or not workspace or len(workspace.encode('utf-8'))>255 or any(ord(c)<32 for c in workspace): raise ValueError('invalid lifecycle application scope')
    run_dir=Path(run_dir); run_dir.mkdir(parents=True,exist_ok=True)
    driver=run_dir/'lifecycle-driver.sql'
    adapter=Path(__file__).with_suffix('.sql').resolve()
    if any(c in str(adapter) for c in ('"','\n','\r')): raise ValueError('unsupported SQL driver path')
    driver.write_text('\n'.join(['SET DEFINE ON','SET VERIFY OFF','SET ECHO OFF','SET FEEDBACK OFF','SET HEADING OFF','SET LINESIZE 32767','SET SERVEROUTPUT ON SIZE UNLIMITED','WHENEVER SQLERROR EXIT FAILURE ROLLBACK','WHENEVER OSERROR EXIT FAILURE ROLLBACK',f'ALTER SESSION SET CURRENT_SCHEMA = {target.schema};','SET TRANSACTION READ ONLY;',f'DEFINE lifecycle_user_hex = {target.expected_user.encode().hex().upper()}',f'DEFINE lifecycle_schema_hex = {target.schema.encode().hex().upper()}',f'@"{adapter}" {workspace.encode("utf-8").hex().upper()} {app_id} CAPTURE','EXIT SUCCESS ROLLBACK','']),encoding='utf-8')
    result=_runner(target,driver,run_dir)
    lines=result.output.splitlines()
    if result.returncode or lines.count('LIFECYCLE_PAYLOAD_BEGIN')!=1 or lines.count('LIFECYCLE_PAYLOAD_END')!=1 or lines.count('LIFECYCLE_VERIFIED')!=1: raise ValueError('lifecycle SQLcl observation is unavailable or incomplete')
    start=lines.index('LIFECYCLE_PAYLOAD_BEGIN'); end=lines.index('LIFECYCLE_PAYLOAD_END')
    if not start<end<lines.index('LIFECYCLE_VERIFIED'): raise ValueError('invalid lifecycle framing')
    payload=''.join(lines[start+1:end])
    if len(payload.encode('utf-8'))>MAX_BYTES: raise ValueError('lifecycle payload exceeds size limit')
    data=json.loads(payload,object_pairs_hook=_pairs)
    validate_snapshot(data)
    if data['workspace']!=workspace or data['appId']!=app_id or data['identity']['session_user']!=target.expected_user or data['identity']['current_schema']!=target.schema: raise ValueError('lifecycle target identity or application mismatch')
    if target.environment!='prod' and looks_like_production_identity(data['identity']['db_unique_name'],data['identity']['service_name']): raise ValueError('lifecycle observed production identity for a non-production target')
    (run_dir/'lifecycle-snapshot.json').write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
    return data


def _automation_restore_driver(target: Target, workspace: str, app_id: int, static_ids: list[str], run_dir: Path) -> Path:
    if not ORACLE_IDENTIFIER.fullmatch(target.schema) or not ORACLE_IDENTIFIER.fullmatch(target.expected_user) or not SQLCL_ALIAS.fullmatch(target.connection) or target.environment not in ('dev','staging','prod'):
        raise ValueError('invalid lifecycle database target')
    if type(app_id) is not int or not 0<app_id<10**18 or not isinstance(workspace,str) or not workspace or len(workspace.encode('utf-8'))>255 or any(ord(c)<32 for c in workspace):
        raise ValueError('invalid lifecycle application scope')
    adapter=Path(__file__).with_suffix('.sql').resolve()
    if any(c in str(adapter) for c in ('"','\n','\r')): raise ValueError('unsupported SQL driver path')
    driver=run_dir/'automation-restore-driver.sql'
    lines=['SET DEFINE ON','SET VERIFY OFF','SET ECHO OFF','SET FEEDBACK OFF','SET HEADING OFF','SET LINESIZE 32767','SET SERVEROUTPUT ON SIZE UNLIMITED','WHENEVER SQLERROR EXIT FAILURE ROLLBACK','WHENEVER OSERROR EXIT FAILURE ROLLBACK',f'ALTER SESSION SET CURRENT_SCHEMA = {target.schema};',f'DEFINE lifecycle_user_hex = {target.expected_user.encode().hex().upper()}',f'DEFINE lifecycle_schema_hex = {target.schema.encode().hex().upper()}',f'@"{adapter}" {workspace.encode("utf-8").hex().upper()} {app_id} RESTORE_CONTEXT','SET DEFINE OFF']
    lines.append('BEGIN')
    for static_id in static_ids:
        identifier_hex=static_id.encode('utf-8').hex().upper()
        lines.extend([
            '  BEGIN',
            f"    APEX_AUTOMATION.ENABLE(p_application_id => {app_id}, p_static_id => UTL_I18N.RAW_TO_CHAR(HEXTORAW('{identifier_hex}'),'AL32UTF8'));",
            f"    DBMS_OUTPUT.PUT_LINE('AUTOMATION_RESTORE_OK:{identifier_hex}');",
            '  EXCEPTION WHEN OTHERS THEN',
            f"    DBMS_OUTPUT.PUT_LINE('AUTOMATION_RESTORE_FAILED:{identifier_hex}:' || TO_CHAR(SQLCODE));",
            '  END;',
        ])
    lines.extend(['END;','/','PROMPT AUTOMATION_RESTORE_COMPLETE','EXIT SUCCESS COMMIT',''])
    driver.write_text('\n'.join(lines),encoding='utf-8')
    return driver


def _automation_statuses(snapshot: Mapping) -> dict[str, str]:
    return {row['staticId']: row['status'] for row in snapshot.get('automations', [])}


def _table_cell(value: str) -> str:
    return value.replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')


def restore_automations(target: Target, workspace: str, app_id: int, before: Mapping, run_dir: Path, *, _runner=run_sqlcl) -> dict:
    validate_snapshot(before)
    if before['workspace'] != workspace or before['appId'] != app_id:
        raise ValueError('pre-import lifecycle snapshot does not match the target application')
    if before['identity']['session_user'] != target.expected_user or before['identity']['current_schema'] != target.schema:
        raise ValueError('pre-import lifecycle snapshot does not match the target identity')
    if target.environment != 'prod' and looks_like_production_identity(before['identity']['db_unique_name'], before['identity']['service_name']):
        raise ValueError('lifecycle observed production identity for a non-production target')
    run_dir=Path(run_dir); run_dir.mkdir(parents=True,exist_ok=True)
    active_ids=sorted(row['staticId'] for row in before['automations'] if row['status'].upper()=='ACTIVE')
    restore_error=None
    if active_ids:
        try:
            driver=_automation_restore_driver(target,workspace,app_id,active_ids,run_dir)
            result=_runner(target,driver,run_dir)
            lines=result.output.splitlines()
            expected={static_id.encode('utf-8').hex().upper() for static_id in active_ids}
            succeeded={line.split(':',1)[1] for line in lines if line.startswith('AUTOMATION_RESTORE_OK:')}
            failed={line.split(':',1)[1].split(':',1)[0] for line in lines if line.startswith('AUTOMATION_RESTORE_FAILED:')}
            context_count=lines.count('LIFECYCLE_CONTEXT_VERIFIED')
            completion_count=lines.count('AUTOMATION_RESTORE_COMPLETE')
            if result.returncode or context_count != 1 or completion_count != 1 or succeeded != expected or failed:
                restore_error='APEX automation enable call did not verify for every pre-import active static ID'
        except (OSError,ValueError,SqlclError):
            restore_error='APEX automation enable session failed; inspect private SQLcl diagnostics'

    try:
        after=capture_lifecycle(target,workspace,app_id,run_dir/'observation-after-restore',_runner=_runner)
    except (ValueError,OSError,SqlclError):
        after=None
    before_status=_automation_statuses(before)
    after_status={} if after is None else _automation_statuses(after)
    all_ids=sorted(before_status.keys() | after_status.keys())
    rows=[{'staticId':identifier,'before':before_status.get(identifier,'ABSENT'),'after':after_status.get(identifier,'UNKNOWN' if after is None else 'ABSENT')} for identifier in all_ids]
    reasons=[]
    if restore_error: reasons.append(restore_error)
    if after is None: reasons.append('post-restore automation read-back is unavailable')
    else:
        for static_id in active_ids:
            if after_status.get(static_id) != 'ACTIVE':
                reasons.append('previously active automation is not active after restore: '+static_id)
    report={'schemaVersion':1,'status':'pass' if not reasons else 'attention','enabledStaticIds':active_ids,'beforeAfter':rows,'reasons':reasons}
    (run_dir/'automation-restore-report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('Automation status before import | after restore')
    print('Static ID | Before | After')
    for row in rows:
        print(_table_cell(row['staticId'])+' | '+_table_cell(row['before'])+' | '+_table_cell(row['after']))
    if not rows: print('(no automations observed)')
    return report


def verify_after_import(target: Target, workspace: str, app_id: int, before: Mapping, run_dir: Path) -> dict:
    try:
        after=capture_lifecycle(target,workspace,app_id,run_dir/'observation-1')
        report=compare_lifecycle(before,after)
        if report['retrySuspension']:
            time.sleep(5)
            after=capture_lifecycle(target,workspace,app_id,run_dir/'observation-2')
            report=compare_lifecycle(before,after)
    except (ValueError,OSError,SqlclError) as exc:
        report={'schemaVersion':1,'status':'unavailable','changes':[],'reasons':[type(exc).__name__+': lifecycle observation failed; inspect private diagnostics']}
    run_dir.mkdir(parents=True,exist_ok=True)
    (run_dir/'lifecycle-report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('capture','verify','restore-automations'))
    for key in ('connection','expected-user','schema','workspace','environment','run-dir'): parser.add_argument('--'+key,required=True)
    parser.add_argument('--app-id',required=True,type=int); parser.add_argument('--before',type=Path)
    parser.add_argument('--summary-path',type=Path)
    args=parser.parse_args(argv)
    report={'status':'unavailable'}
    try:
        target=Target(args.environment,args.connection,args.expected_user,args.schema,'lifecycle')
        if args.action=='capture':
            capture_lifecycle(target,args.workspace,args.app_id,Path(args.run_dir)); report={'status':'pass'}
        elif args.action=='verify':
            if args.before is None: raise ValueError('--before is required')
            report=verify_after_import(target,args.workspace,args.app_id,read_snapshot(args.before),Path(args.run_dir))
            require_verified_lifecycle(report)
        else:
            if args.before is None: raise ValueError('--before is required')
            report=restore_automations(target,args.workspace,args.app_id,read_snapshot(args.before),Path(args.run_dir))
            if report['status']!='pass': raise ValueError('automation restore did not verify: '+'; '.join(report['reasons']))
        print('Application lifecycle '+args.action+' verified.'); return 0
    except (ValueError,OSError,SqlclError) as exc:
        print('lifecycle: '+str(exc),file=sys.stderr); return 2
    finally:
        if args.summary_path:
            args.summary_path.write_text(json.dumps({'schemaVersion':1,'sourceVerified':True,'lifecycleStatus':report['status']})+'\n',encoding='utf-8')


if __name__=='__main__': raise SystemExit(main())
