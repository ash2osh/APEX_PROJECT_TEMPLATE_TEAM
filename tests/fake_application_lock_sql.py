"""SQLcl protocol fixture for the native-lock phases of command tests."""
import json
import os
import re
import sys
from pathlib import Path


def main():
    drivers=[Path(arg[1:]) for arg in sys.argv[1:] if arg.startswith('@')]
    if not drivers or not drivers[0].is_file(): return 3
    if drivers[0].name == 'export_apps.sql':
        args=sys.argv[1:]; index=args.index('@'+str(drivers[0])); schema,app_id,environment=args[index+1:index+4]
        root=drivers[0].parent.parent
        environment={'production':'prod','development':'dev','test':'dev'}.get(environment,environment)
        descriptors=list((root/'apps').glob(f'*/{app_id}/deployments/{environment}.json'))
        if len(descriptors)==1:
            observed=json.loads(descriptors[0].read_text())
            if os.environ.get('FAKE_DEPLOYMENT_MISMATCH'):
                observed['workspace']['name']='WRONG_WORKSPACE'
            Path('.apex-deployment-state.json').write_text(json.dumps(observed))
            if 'sessionStateProtection' in observed['app']:
                generated=Path('apps')/schema/app_id/'deployments/default.json'
                generated.parent.mkdir(parents=True,exist_ok=True)
                generated.write_text(json.dumps({'app':{'id':int(app_id),'sessionStateProtection':observed['app']['sessionStateProtection']}}))
        return 3
    sql=drivers[0].read_text(encoding='utf-8')
    match=re.search(r'^prompt APEX_LOCK_VERIFIED:(\w+)$',sql,re.MULTILINE)
    if not match: return 3
    phase=match[1]
    directory=drivers[0].parent.parent
    root=directory
    while root.name not in ('.sync-state', 'scripts') and root.parent!=root: root=root.parent
    state_path=root.parent/'fake-lock-state.json'
    calls=os.environ.get('FAKE_LOCK_CALLS')
    if calls:
        with open(calls,'a',encoding='utf-8') as stream: stream.write(phase+'\n')
    mode=os.environ.get('FAKE_APEX_LOCK_MODE','')
    live_file=Path(os.environ.get('FAKE_STATE_DIR',str(directory)))/'live.txt'
    if phase=='acquire' and live_file.exists() and live_file.read_text().strip()=='NOT_FOUND':
        mode='absent-app'
    if mode in {'unknown-user','no-developer-role','absent-app','working-copy'} or (phase=='acquire' and mode in {'same-owner','other-owner'}):
        code={'unknown-user':20064,'no-developer-role':20064,'absent-app':20065,'working-copy':20066}.get(mode,20067)
        print(f'ORA-{code}: native application lock refused: '+mode)
        return 1
    state=json.loads(state_path.read_text()) if state_path.exists() else None
    if phase=='acquire':
        if state:
            print('ORA-20067: Application already locked')
            return 1
        record=json.loads((directory/'recovery.json').read_text())
        state=dict(record['evidence'], locked_on='2026-10-08T11:00:00.000000')
    elif phase in {'check','release'}:
        if mode=='drop-after-import' and (Path(os.environ['FAKE_STATE_DIR'])/'import-count.txt').read_text().strip()!='0':
            state=None
        record=json.loads((directory/'recovery.json').read_text())
        if state!=record['evidence']:
            print('ORA-20069: Application lock ownership changed')
            return 1
        if phase=='release': state=None
    elif phase=='unlock': state=None
    elif phase=='user':
        user=re.search(r"upper\(user_name\)=upper\('((?:[^']|'')*)'\)",sql)[1].replace("''", "'")
        print('APEX_LOCK_STATE:'+json.dumps({'developer':user.upper()}))
        print('APEX_LOCK_VERIFIED:user')
        return 0
    state_path.write_text(json.dumps(state))
    print('APEX_LOCK_STATE:'+json.dumps(state))
    print('APEX_LOCK_VERIFIED:'+phase)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
