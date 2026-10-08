#!/usr/bin/env python3
"""Public APEX application locks with durable, checkout-local recovery proof."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.db_targets import ORACLE_IDENTIFIER, SQLCL_ALIAS, Target, looks_like_production_identity
from scripts.sqlcl_session import bash_command, run_sqlcl


class LockError(ValueError):
    """Lock ownership or a write result could not be verified."""


@dataclass(frozen=True)
class LockEvidence:
    app_id: int
    workspace: str
    schema: str
    developer: str
    run_id: str
    locked_on: str
    comment: str


ERRORS = re.compile(r'(?im)^\s*(?:Unknown Command\b|Error report -|Error starting at line\b|(?:ORA|PLS|SP2|TNS|SQL)-[0-9]+:)')
TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\Z')


def literal(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c)<32 for c in value):
        raise LockError('lock values must be nonempty single-line text; set APEX_WORKSPACE_USERNAME explicitly')
    return "'" + value.replace("'", "''") + "'"


def validate_target(target, app_id, workspace, developer, run_dir):
    if target.environment!='dev' or target.classification not in {'development', 'test'}:
        raise LockError('application locks are restricted to DEV/test targets')
    if not SQLCL_ALIAS.fullmatch(target.connection) or looks_like_production_identity(target.connection):
        raise LockError('use a saved development SQLcl connection')
    if not ORACLE_IDENTIFIER.fullmatch(target.expected_user) or not ORACLE_IDENTIFIER.fullmatch(target.schema):
        raise LockError('expected user and parsing schema must be uppercase Oracle identifiers')
    if type(app_id) is not int or not 0<app_id<10**18:
        raise LockError('application ID must be a positive integer of at most 18 digits')
    literal(workspace)
    literal(developer)
    if len(developer.encode('utf-8'))>255:
        raise LockError('APEX_WORKSPACE_USERNAME is too long')
    directory=Path(run_dir)
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise LockError('lock recovery directory cannot contain symlinks')
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)


def app_guard(app_id, workspace, schema):
    return f'''
  select count(*) into v_count from apex_applications
   where application_id={app_id} and workspace={literal(workspace)} and owner={literal(schema)};
  if v_count<>1 then raise_application_error(-20065, 'Application target not found or workspace/schema mismatch'); end if;
  select locked_by, lock_comment,
    to_char(cast(locked_on as timestamp), 'YYYY-MM-DD"T"HH24:MI:SS.FF6'), is_working_copy
    into v_owner, v_comment, v_locked_on, v_copy from apex_applications where application_id={app_id};
  if nvl(v_copy,'No')<>'No' then raise_application_error(-20066, 'Working copies are deferred'); end if;
'''


def expand(target, app_id, workspace, developer, run_id, phase, body):
    text=Path(__file__).with_suffix('.sql').read_text(encoding='utf-8')
    values={'EXPECTED_USER':literal(target.expected_user), 'APP_ID':str(app_id),
            'WORKSPACE':literal(workspace), 'DEVELOPER':literal(developer),
            'RUN_ID':literal(run_id), 'PHASE':phase}
    for key,value in values.items():
        text=text.replace('@@'+key+'@@', value)
    return text.replace('@@BODY@@', body)


def acquisition_sql(target, app_id, workspace, developer, run_id, comment):
    body=app_guard(app_id,workspace,target.schema)+f'''
  if v_owner is not null then
    raise_application_error(-20067, 'Application already locked; existing same-owner and other-owner locks both require review');
  end if;
  apex_application_admin.lock_application(p_application_id => {app_id},
    p_lock_as_user => v_user, p_lock_comment => {literal(comment)});
  commit;
  select locked_by, lock_comment into v_owner, v_comment from apex_applications where application_id={app_id};
  if v_owner is null or upper(v_owner)<>upper(v_user) or v_comment is null or v_comment<>{literal(comment)} then
    raise_application_error(-20068, 'Acquired application lock could not be verified');
  end if;
  dbms_output.put_line('APEX_LOCK_STATE:' || state_json);
'''
    return expand(target,app_id,workspace,developer,run_id,'acquire',body)


def evidence_guard(evidence):
    return f'''
  if v_owner is null or upper(v_owner)<>upper(v_user)
    or v_comment is null or v_comment<>{literal(evidence.comment)}
    or v_locked_on is null or v_locked_on<>{literal(evidence.locked_on)} then
    raise_application_error(-20069, 'Application lock ownership changed; preserve recovery evidence and reconcile');
  end if;
'''


def assertion_sql(target, evidence):
    """In-session ownership assertion, also safe for an import driver to include."""
    text=expand(target,evidence.app_id,evidence.workspace,evidence.developer,evidence.run_id,'check',
                app_guard(evidence.app_id,evidence.workspace,target.schema)+evidence_guard(evidence))
    # Imports need the declaration and SET DEFINE OFF, without a prompt or EXIT.
    return text[:text.index('prompt APEX_LOCK_VERIFIED:')]


def save_record(directory, target, status, evidence):
    path=Path(directory)/'recovery.json'
    if path.is_symlink():
        raise LockError('lock recovery record cannot be a symlink')
    data={'schemaVersion':1,'target':asdict(target),'status':status,'evidence':asdict(evidence)}
    temporary=path.with_name('recovery-'+uuid.uuid4().hex+'.tmp')
    try:
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            json.dump(data,stream,indent=2,ensure_ascii=False); stream.write('\n')
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def execute(target, directory, phase, sql):
    attempt=Path(directory)/(phase+'-'+uuid.uuid4().hex)
    attempt.mkdir(mode=0o700)
    driver=attempt/'driver.sql'
    driver.write_text(sql,encoding='utf-8',newline='\n')
    try:
        output=run_sqlcl(target,driver,attempt).output
        states=[line[len('APEX_LOCK_STATE:'):] for line in output.splitlines() if line.startswith('APEX_LOCK_STATE:')]
        if ERRORS.search(output) or len(states)!=1 or 'APEX_LOCK_VERIFIED:'+phase not in output.splitlines():
            raise LockError('SQLcl did not unambiguously verify application lock state')
        return json.loads(states[0])
    except (Exception, KeyboardInterrupt) as error:
        errors = getattr(error, 'output', '')
        refusals = {'ORA-20064': 'APEX_WORKSPACE_USERNAME must name an existing workspace developer/admin',
                    'ORA-20065': 'application target not found or workspace/schema mismatch',
                    'ORA-20066': 'working copies are deferred',
                    'ORA-20067': 'application already locked; review the existing lock before retrying',
                    'ORA-20069': 'application lock ownership changed; preserve recovery evidence and reconcile'}
        for code, message in refusals.items():
            if code+':' in errors:
                raise LockError(f'{message}; recovery and diagnostics retained at {directory}') from error
        raise LockError(f'{phase} result unavailable; recovery and diagnostics retained at {directory}') from error


def parse_evidence(data):
    try:
        evidence=LockEvidence(**data)
        if type(evidence.app_id) is not int or not 0<evidence.app_id<10**18:
            raise ValueError()
        for value in (evidence.workspace,evidence.schema,evidence.developer,evidence.comment): literal(value)
        if not ORACLE_IDENTIFIER.fullmatch(evidence.schema) or not re.fullmatch('[0-9a-f]{32}',evidence.run_id):
            raise ValueError()
        if not TIMESTAMP.fullmatch(evidence.locked_on): raise ValueError()
        return evidence
    except (TypeError,ValueError) as error:
        raise LockError('invalid application lock evidence') from error


def acquire_lock(target, app_id, workspace, developer, run_dir, *, comment=''):
    validate_target(target,app_id,workspace,developer,run_dir)
    if (Path(run_dir)/'recovery.json').exists():
        raise LockError('use a fresh recovery directory; existing acquisition evidence must be preserved')
    if comment:
        literal(comment)
    run_id=uuid.uuid4().hex
    tagged_comment='apex-team:'+run_id+(' '+comment if comment else '')
    if len(tagged_comment.encode('utf-8'))>3000:
        raise LockError('application lock comment is too long')
    intent=LockEvidence(app_id,workspace,target.schema,developer,run_id,'',tagged_comment)
    save_record(run_dir,target,'acquisition-unknown',intent)
    observed=execute(target,run_dir,'acquire',acquisition_sql(target,app_id,workspace,developer,run_id,tagged_comment))
    evidence=parse_evidence(observed)
    if (evidence.app_id,evidence.workspace,evidence.schema,evidence.run_id,evidence.comment)!=\
       (app_id,workspace,target.schema,run_id,tagged_comment) or evidence.developer.upper()!=developer.upper():
        raise LockError('acquired lock evidence differs from the requested target; preserve recovery evidence')
    # Persist exact proof before the next session, so failed readback is recoverable.
    save_record(run_dir,target,'acquisition-unknown',evidence)
    check_lock(target,evidence,run_dir)
    save_record(run_dir,target,'held',evidence)
    (Path(run_dir)/'assert-lock.sql').write_text(assertion_sql(target,evidence),encoding='utf-8',newline='\n')
    return evidence


def validate_evidence_target(target,evidence,run_dir):
    parse_evidence(asdict(evidence))
    validate_target(target,evidence.app_id,evidence.workspace,evidence.developer,run_dir)
    if target.schema!=evidence.schema:
        raise LockError('lock evidence targets a different parsing schema')
    record_path=Path(run_dir)/'recovery.json'
    if record_path.exists():
        if record_path.is_symlink(): raise LockError('lock recovery record cannot be a symlink')
        try: record=json.loads(record_path.read_text(encoding='utf-8'))
        except (OSError,ValueError) as error: raise LockError('invalid lock recovery record') from error
        if record.get('target')!=asdict(target) or record.get('evidence')!=asdict(evidence):
            raise LockError('lock recovery evidence or SQLcl target differs from this operation')


def check_lock(target,evidence,run_dir):
    validate_evidence_target(target,evidence,run_dir)
    body=app_guard(evidence.app_id,evidence.workspace,target.schema)+evidence_guard(evidence)
    body+="  dbms_output.put_line('APEX_LOCK_STATE:' || state_json);\n"
    state=execute(target,run_dir,'check',expand(target,evidence.app_id,evidence.workspace,evidence.developer,evidence.run_id,'check',body))
    if state!=asdict(evidence):
        raise LockError('application lock ownership changed; preserve recovery evidence and reconcile')


def release_lock(target,evidence,run_dir):
    validate_evidence_target(target,evidence,run_dir)
    save_record(run_dir,target,'release-unknown',evidence)
    body=app_guard(evidence.app_id,evidence.workspace,target.schema)+evidence_guard(evidence)+f'''
  apex_application_admin.unlock_application(p_application_id => {evidence.app_id}, p_unlock_as_user => v_user);
  commit;
  if state_json<>'null' then raise_application_error(-20070,'Application unlock could not be verified'); end if;
  dbms_output.put_line('APEX_LOCK_STATE:null');
'''
    state=execute(target,run_dir,'release',expand(target,evidence.app_id,evidence.workspace,evidence.developer,evidence.run_id,'release',body))
    if state is not None: raise LockError('application unlock could not be verified')
    save_record(run_dir,target,'released',evidence)


def manual_unlock_sql(target,app_id,workspace,developer,run_id):
    body=app_guard(app_id,workspace,target.schema)+f'''
  if v_owner is not null and upper(v_owner)<>upper(v_user) then
    raise_application_error(-20071, 'Application is locked by another developer');
  end if;
  if v_owner is not null then
    apex_application_admin.unlock_application(p_application_id => {app_id}, p_unlock_as_user => v_user);
    commit;
  end if;
  if state_json<>'null' then raise_application_error(-20070,'Application unlock could not be verified'); end if;
  dbms_output.put_line('APEX_LOCK_STATE:null');
'''
    return expand(target,app_id,workspace,developer,run_id,'unlock',body)


def user_validation_sql(target,workspace,developer):
    sql=expand(target,0,workspace,developer,uuid.uuid4().hex,'user',
               "dbms_output.put_line('APEX_LOCK_STATE:' || json_object('developer' value v_user));")
    return re.sub(r'  function state_json.*?  end;\n','',sql,count=1,flags=re.DOTALL)


def require_client(directory):
    repo=Path(__file__).resolve().parents[1]
    result=subprocess.run([bash_command(),'-c',
        'REPO_ROOT=$1; source "$1/scripts/sqlcl_safe.sh"; sqlcl_require_apex_version "$2"',
        'application-lock',str(repo),str(Path(directory)/'version')],capture_output=True,text=True,timeout=60,check=False)
    if result.returncode:
        raise LockError('SQLcl 26.3.0.0 compatibility could not be verified; inspect private version diagnostics')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('acquire','check','release','unlock','user'))
    parser.add_argument('--connection',required=True)
    parser.add_argument('--expected-user',required=True)
    parser.add_argument('--schema',required=True)
    parser.add_argument('--workspace',required=True)
    parser.add_argument('--developer',required=True)
    parser.add_argument('--app-id',type=int,required=True)
    parser.add_argument('--classification',default='development')
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--comment',default='')
    args=parser.parse_args(argv)
    target=Target('dev',args.connection,args.expected_user,args.schema,args.classification)
    try:
        validate_target(target,args.app_id,args.workspace,args.developer,args.run_dir)
        require_client(args.run_dir)
        if args.operation=='acquire':
            acquire_lock(target,args.app_id,args.workspace,args.developer,args.run_dir,comment=args.comment)
        elif args.operation=='user':
            state=execute(target,args.run_dir,'user',user_validation_sql(target,args.workspace,args.developer))
            if not isinstance(state,dict) or not isinstance(state.get('developer'),str) or state['developer'].upper()!=args.developer.upper():
                raise LockError('workspace user validation did not return the expected developer')
        elif args.operation=='unlock':
            run_id=uuid.uuid4().hex
            intent=LockEvidence(args.app_id,args.workspace,args.schema,args.developer,run_id,'','manual owner-filtered unlock')
            save_record(args.run_dir,target,'release-unknown',intent)
            state=execute(target,args.run_dir,'unlock',manual_unlock_sql(target,args.app_id,args.workspace,args.developer,run_id))
            if state is not None: raise LockError('manual application unlock could not be verified')
            save_record(args.run_dir,target,'released',intent)
        else:
            path=args.run_dir/'recovery.json'
            if path.is_symlink(): raise LockError('lock recovery record cannot be a symlink')
            record=json.loads(path.read_text(encoding='utf-8'))
            evidence=parse_evidence(record['evidence'])
            if evidence.app_id!=args.app_id or evidence.workspace!=args.workspace or evidence.developer.upper()!=args.developer.upper():
                raise LockError('recovery record differs from requested application/workspace/developer')
            (check_lock if args.operation=='check' else release_lock)(target,evidence,args.run_dir)
        if args.operation=='user':
            print(f'Workspace developer verified: {state["developer"]} / {args.workspace}')
        else:
            print(f'Application lock {args.operation} verified; recovery: {args.run_dir / "recovery.json"}')
        return 0
    except (LockError,OSError,ValueError,KeyError,subprocess.TimeoutExpired) as error:
        print(f'application lock error: {error}',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
