#!/usr/bin/env python3
"""Qualify an explicitly identified local APEX target with read-only observations."""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
from pathlib import Path
if __package__ in (None,''):
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.probe_apex_26_2 import Probe, ProbeTarget, ProbeError, client_version
from scripts.apex_compatibility import require_sqlcl_version
from scripts.application_lifecycle import capture_lifecycle
from scripts.schema_catalog import capture_inventory
from scripts.db_targets import Target
from scripts.sqlcl_session import run_sqlcl

ROOT=Path(__file__).resolve().parents[1]


def _output_path(output: Path, repo_root: Path) -> Path:
    root=repo_root.resolve(); scratch=root/'scratch'; candidate=output.absolute()
    try: candidate.relative_to(scratch)
    except ValueError as exc: raise ValueError('--output must be a fresh directory under this repository scratch/') from exc
    if candidate==scratch or candidate.exists() or candidate.is_symlink(): raise ValueError('qualification requires a fresh output directory')
    for path in (candidate,*candidate.parents):
        if path==root: break
        if path.is_symlink(): raise ValueError('qualification output cannot contain symlinked parents')
    if not candidate.resolve().is_relative_to(scratch): raise ValueError('qualification output escaped scratch/')
    return candidate


def qualify(target: ProbeTarget, output: Path, *, repo_root=ROOT, runner=run_sqlcl, version_reader=client_version) -> dict:
    directory=_output_path(Path(output),Path(repo_root))
    report={'schemaVersion':1,'mode':'read-only','status':'pass','checks':[],
            'limitations':['Observations do not qualify imports or native Windows behavior.', 'Empty automation/workflow/task arrays prove scoped readability, not positive runtime behavior.']}
    probe=Probe(target,directory/'identity',runner=runner,version_reader=version_reader)
    # Every adapter invocation repeats exact host/service/user/schema/context guards.
    def guarded(selected,driver,run_dir):
        original=driver.read_text(encoding='utf-8')
        # Catalog capture selects a session schema; it creates no schema objects.
        original=re.sub(r'(?im)^\s*SET TRANSACTION READ ONLY;\s*$', '', original)
        original=re.sub(r'(?im)^\s*ALTER SESSION SET CURRENT_SCHEMA = '+re.escape(selected.schema)+r';\s*$', '', original)
        if re.search(r'(?im)^\s*(?:apex\s+(?:export|import)|commit\b|(?:create|alter|drop|truncate)\s)',original): raise ValueError('read-only qualification rejected a write command')
        preamble=probe.preamble().replace('declare v_group number;','SET TRANSACTION READ ONLY;\ndeclare v_group number;')
        driver.write_text(preamble+original,encoding='utf-8')
        return runner(selected,driver,run_dir)
    probe.runner=guarded
    def check(name,operation):
        started=time.perf_counter()
        try:
            details=operation()
            report['checks'].append({'name':name,'status':'pass','durationSeconds':round(time.perf_counter()-started,4),**(details or {})})
            return True
        except Exception as exc:
            status='fail' if isinstance(exc,ProbeError) and name=='identity' else 'unavailable'
            report['status']=status
            report['checks'].append({'name':name,'status':status,'durationSeconds':round(time.perf_counter()-started,4),'reason':type(exc).__name__+': inspect private diagnostics'})
            return False
    directory.mkdir(parents=True,mode=0o700)
    version=None
    def version_check():
        nonlocal version
        version=version_reader(directory/'version'); require_sqlcl_version(version)
        return {'sqlclVersion':version}
    def identity_check():
        observed=probe.identify()
        if type(observed.get('appCount')) is not int or observed['appCount'] != 1:
            raise ProbeError('read-only runtime qualification requires an existing application')
        return {'identity':observed}
    if check('sqlcl-version',version_check) and check('identity',identity_check):
        selected=Target('dev',target.connection,target.expected_user,target.schema,'development')
        def catalog_check():
            inventory=capture_inventory(selected,directory/'catalog',_runner=guarded)
            return {'scope':'real owner inventory','objectCount':len(inventory.objects),'transport':'gzip-base64-v1'}
        check('catalog',catalog_check)
        def lifecycle_check():
            observed=capture_lifecycle(selected,target.workspace,target.app_id,directory/'lifecycle',_runner=guarded)
            counts={kind:len(observed[kind]) for kind in ('automations','workflows','tasks')}
            return {'appId':target.app_id,'counts':counts,'positiveCoverage':{kind:count>0 for kind,count in counts.items()}}
        check('lifecycle',lifecycle_check)
    (directory/'qualification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('connection','expected-user','schema','workspace','server-host','service'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--app-id',required=True,type=int,help='existing application to observe; never exported or imported')
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        target=ProbeTarget(args.connection,args.expected_user,args.schema,args.workspace,args.app_id,args.server_host,args.service)
        report=qualify(target,args.output)
        print(json.dumps(report,indent=2)); return 0 if report['status']=='pass' else 2
    except ValueError as exc:
        print('qualification: '+str(exc),file=sys.stderr); return 2

if __name__=='__main__':raise SystemExit(main())
