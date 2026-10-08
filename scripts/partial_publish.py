#!/usr/bin/env python3
"""Existing-page selection, fresh-snapshot staging and exact verification."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.source_evidence import tree_hashes
from scripts.upgrade_apexlang import preserve_authored


def page_lines(path: Path) -> list[str]:
    source = path.read_text(encoding='utf-8')
    structural = []
    fenced = False
    for line in source.splitlines():
        if line.lstrip().startswith('```'):
            fenced = not fenced
            continue
        if not fenced and line.strip():
            structural.append(line)
    if fenced or len(structural) < 2 or structural[-1] != ')' or any(not line.startswith(' ') for line in structural[1:-1]):
        raise ValueError('partial selection requires canonical page-only files without extra top-level components')
    return structural


def page_id(path: Path) -> int:
    matches = re.findall(r'^page ([0-9]+) \($', page_lines(path)[0])
    if len(matches) != 1 or not 0 < int(matches[0]) < 10**18:
        raise ValueError('selection requires exactly one existing non-global page per file')
    return int(matches[0])


def select_page_files(source_dir: Path, files: list[str], baseline_hashes: dict[str, str]) -> tuple[Path, ...]:
    if not files or len(files) != len(set(files)):
        raise ValueError('selection requires unique existing page files')
    current = tree_hashes(source_dir)
    selected = []
    ids = set()
    for name in files:
        relative = PurePosixPath(name)
        if relative.is_absolute() or name != relative.as_posix() or len(relative.parts) != 2 or relative.parts[0] != 'pages' or re.fullmatch(r'[A-Za-z0-9_-]+\.apx', relative.name) is None:
            raise ValueError('selection requires app-relative pages/*.apx paths without links or traversal')
        if name not in baseline_hashes or name not in current:
            raise ValueError('selection refuses new or deleted pages; export canonical source first')
        path = source_dir / name
        identifier = page_id(path)
        if identifier in ids:
            raise ValueError('selection is ambiguous: duplicate page IDs')
        ids.add(identifier); selected.append(path)
    differences = {name for name in current.keys() | baseline_hashes.keys() if current.get(name) != baseline_hashes.get(name)}
    if differences - set(files):
        raise ValueError('partial publishing refuses unselected local edits, additions or deletions')
    return tuple(selected)


def selected_relative(path: Path) -> str:
    return 'pages/' + path.name


def require_selected_pages_unchanged(live_source: Path, selected: tuple[Path, ...], baseline_hashes: dict[str, str]) -> None:
    current = tree_hashes(live_source)
    for path in selected:
        name = selected_relative(path)
        if current.get(name) is None or current[name] != baseline_hashes.get(name):
            raise ValueError('selected page changed in Builder; export and reconcile before partial publishing')
        old_alias = [line for line in page_lines(live_source / name) if line.startswith('    alias:')]
        new_alias = [line for line in page_lines(path) if line.startswith('    alias:')]
        if len(old_alias) > 1 or len(new_alias) > 1 or old_alias != new_alias:
            raise ValueError('partial publication refuses page alias changes; use a coordinated full change')
        if page_id(live_source / name) != page_id(path):
            raise ValueError('selected page identity differs from the live snapshot')


def stage_partial_source(live_source: Path, local_source: Path, selected: tuple[Path, ...], stage_dir: Path) -> Path:
    tree_hashes(live_source); tree_hashes(local_source)
    if stage_dir.exists() or stage_dir.is_symlink():
        raise ValueError('partial staging requires a fresh directory')
    shutil.copytree(live_source, stage_dir)
    preserve_authored(local_source, stage_dir)
    for path in selected:
        if path.parent != local_source / 'pages':
            raise ValueError('selected file is outside the local app')
        shutil.copyfile(path, stage_dir / selected_relative(path))
    return stage_dir


def verify_partial_source(expected: Path, observed: Path, live_updated_on: str | None) -> None:
    left, right = tree_hashes(expected), tree_hashes(observed)
    if left.keys() != right.keys():
        raise ValueError('whole-app source file set changed unexpectedly during partial publication')
    for name in left:
        if name == 'deployments/default.json':
            a = json.loads((expected / name).read_text(encoding='utf-8'))
            b = json.loads((observed / name).read_text(encoding='utf-8'))
            old = a.get('app', {}).get('sessionStateProtection', {}).get('allowUrlsCreatedAfter')
            new = b.get('app', {}).get('sessionStateProtection', {}).get('allowUrlsCreatedAfter')
            if old != new:
                if not isinstance(new, str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', new) is None or new != live_updated_on:
                    raise ValueError('URL cutoff change does not match the qualified live version-update timestamp')
                for descriptor in (a, b):
                    protection = descriptor.get('app', {}).get('sessionStateProtection', {})
                    protection.pop('allowUrlsCreatedAfter', None)
                    if not protection:
                        descriptor.get('app', {}).pop('sessionStateProtection', None)
            if a != b:
                raise ValueError('unrelated effective deployment changed during partial publication')
        elif left[name] != right[name]:
            raise ValueError('whole-app source bytes changed unexpectedly during partial publication: ' + name)


def selected_lock_rows(rows, selected: tuple[Path, ...], app_id: int, workspace: str, owner: str) -> list[dict]:
    from scripts.source_evidence import validate_page_locks
    identifiers = {page_id(path) for path in selected}
    observed = [row for row in validate_page_locks(rows, app_id) if row['pageId'] in identifiers]
    if any(row['workspace'] != workspace or row['owner'] != owner for row in observed):
        raise ValueError('selected page is locked by another workspace developer; reconcile in Builder')
    return observed


def require_no_notice_cutoff(live: Path, selected_descriptor: dict | None = None) -> None:
    """Use only the explicitly configured cutoff contract qualified in Chrome.

    Generated cutoffs can be materialized/advanced by version updates. They
    require coordinated publication until their runtime effects are qualified.
    """
    deployment = json.loads((live / 'deployments/default.json').read_text(encoding='utf-8'))
    cutoff = deployment.get('app', {}).get('sessionStateProtection', {}).get('allowUrlsCreatedAfter')
    configured = (selected_descriptor or {}).get('app', {}).get('sessionStateProtection', {}).get('allowUrlsCreatedAfter')
    protection = deployment.get('app', {}).get('sessionStateProtection', {})
    configured_protection = (selected_descriptor or {}).get('app', {}).get('sessionStateProtection', {})
    salt, configured_salt = protection.get('checksumSalt'), configured_protection.get('checksumSalt')
    if not isinstance(salt, str) or not isinstance(configured_salt, str) or salt.upper() != configured_salt.upper():
        raise ValueError('no-team-notice requires an explicit preserved checksum salt; use coordinated partial publication for an implicit salt')
    if not isinstance(cutoff, str) or configured != cutoff:
        raise ValueError('no-team-notice requires an explicit preserved URL cutoff; use coordinated partial publication for an unmanaged/generated cutoff')


def publish_partial(source: Path, app_id: int, files: list[str], workspace: str,
                    run_dir: Path, backend, developer_name: str,
                    *, no_team_notice: bool = False) -> Path:
    """Return a verified canonical stage, leaving original files untouched.

    Installation requires an exact pre-import source receipt under the shared
    mirror lock. Any attempted but unverified write retains its native app lock.
    """
    from datetime import date
    from scripts.source_evidence import read_content_baseline, content_evidence
    from scripts.record_export_state import marker_payload
    from scripts.stamp_publish_version import stamp_application, DEVELOPER_NAME
    from scripts.upgrade_apexlang import normalize_source
    from scripts.validate_app_source import validate_import_effects
    from scripts.check_mirror_source import baseline_hash
    if DEVELOPER_NAME.fullmatch(developer_name) is None:
        raise ValueError('partial publication requires the configured uppercase DEVELOPER_NAME')
    baseline = read_content_baseline(source, app_id)
    selected = select_page_files(source, files, baseline)
    original = tree_hashes(source)
    original_marker = baseline_hash(source)
    marker = json.loads((source / 'apex-team-export.json').read_text(encoding='utf-8'))
    if run_dir.exists() or run_dir.is_symlink():
        raise ValueError('partial publication requires fresh private diagnostics')
    run_dir.mkdir(parents=True, mode=0o700)
    shutil.copytree(source, run_dir / 'source-before')
    report = {'applicationId': app_id, 'selectedFiles': list(files), 'status': 'started',
              'importAttempted': False, 'lockRetained': False, 'noTeamNoticeRequested': no_team_notice}
    held = False
    try:
        backend.validate_target()
        backend.acquire(); held = True
        first = backend.export('live-first')
        first_revision = backend.last_revision
        live = backend.export('live-second')
        normalize_source(first); normalize_source(live)
        content_evidence(live)
        if tree_hashes(first) != tree_hashes(live) or first_revision != backend.last_revision:
            raise ValueError('live application snapshot was unstable before partial publication')
        require_selected_pages_unchanged(live, selected, baseline)
        locks = selected_lock_rows(backend.last_page_locks, selected, app_id, workspace, backend.evidence.developer)
        if no_team_notice:
            captured = marker.get('pageLocks')
            if captured is None:
                raise ValueError('no-team-notice requires Builder locks captured in the baseline before editing')
            previous = selected_lock_rows(captured, selected, app_id, workspace, backend.evidence.developer)
            if len(previous) != len(selected) or previous != locks:
                raise ValueError('no-team-notice requires unchanged exclusive Builder locks on every selected page')
            require_no_notice_cutoff(live, backend.descriptor)
        backend.verify_effective(source, live)
        stage = stage_partial_source(live, source, selected, run_dir / 'import-input')
        validate_import_effects(stage)
        version = stamp_application(stage / 'application.apx', developer_name, date.today().isoformat())
        backend.validate_source(stage)
        if tree_hashes(source) != original or baseline_hash(source) != original_marker:
            raise ValueError('local source changed during partial preflight; original retained')
        backend.check()
        report['importAttempted'] = True
        backend.import_pages(stage, tuple(stage / selected_relative(path) for path in selected), version, locks)
        observed = backend.export('after-import')
        normalize_source(observed)
        preserve_authored(source, observed)
        verify_partial_source(stage, observed, backend.last_revision.last_updated_on)
        backend.verify_effective(source, observed)
        if backend.last_revision.version != version:
            raise ValueError('public application version did not match the selected publish tag')
        after_locks = selected_lock_rows(backend.last_page_locks, selected, app_id, workspace, backend.evidence.developer)
        if after_locks != locks:
            raise ValueError('selected Builder page locks changed during partial publication')
        if tree_hashes(source) != original or baseline_hash(source) != original_marker:
            raise ValueError('local source changed during partial publication; baseline retained')
        backend.check(); backend.release(); held = False
        canonical = run_dir / 'canonical'
        shutil.copytree(observed, canonical)
        new_marker = marker_payload(app_id, backend.last_revision, canonical, backend.last_page_locks)
        (canonical / 'apex-team-export.json').write_text(json.dumps(new_marker, indent=2) + '\n', encoding='utf-8')
        report.update(status='verified', noTeamNoticeEligible=no_team_notice,
                      changedLiveFiles=sorted(name for name in baseline.keys() | tree_hashes(live).keys()
                                              if baseline.get(name) != tree_hashes(live).get(name)))
        return canonical
    except BaseException:
        report['status'] = 'failed'
        if held and not report['importAttempted']:
            try:
                backend.check(); backend.release(); held = False
            except BaseException:
                report['lockReleaseFailed'] = True
        report['lockRetained'] = held
        raise
    finally:
        (run_dir / 'partial.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


def build_partial_driver(selection: tuple[Path, ...], descriptor: Path, app_id: int, version: str) -> str:
    from scripts.upgrade_apexlang import quoted
    if not selection or type(app_id) is not int or not 0 < app_id < 10**18:
        raise ValueError('partial driver requires selected pages and a numeric app ID')
    root = selection[0].parent.parent
    if descriptor != root / 'deployments/dev.json' or any(path.parent != root / 'pages' for path in selection):
        raise ValueError('partial driver paths must belong to the same DEV input tree')
    if any(ord(character) < 32 for character in version) or len(version.encode('utf-8')) > 255:
        raise ValueError('partial version is invalid')
    paths = ' '.join(quoted(path) for path in selection)
    helper = Path(__file__).with_suffix('.sql')
    return (f'apex import -input {quoted(root)} -deployment {quoted(descriptor)} -files {paths}\n'
            f'set define on\n@{quoted(helper)} {app_id} {version.encode("utf-8").hex().upper()}\nset define off\n')


def page_lock_guard(app_id: int, selected: tuple[Path, ...], locks: list[dict]) -> str:
    from scripts.upgrade_apexlang import literal
    observed = {row['pageId']: row for row in locks}
    statements = []
    for identifier in sorted(page_id(path) for path in selected):
        query = f'select count(*) into n from apex_application_locked_pages where application_id={app_id} and page_id={identifier}'
        row = observed.get(identifier)
        if row:
            comment = 'lock_comment is null' if row['comment'] is None else "lock_comment=utl_i18n.raw_to_char(hextoraw('" + row['comment'].encode('utf-8').hex() + "'),'AL32UTF8')"
            query += f" and lock_id={row['lockId']} and workspace={literal(row['workspace'])} and locked_by={literal(row['owner'])} and to_char(locked_on,'YYYY-MM-DD\"T\"HH24:MI:SS')={literal(row['lockedOn'])} and {comment}"
        statements.append(query + f";if n<>{1 if row else 0} then raise_application_error(-20084,'Selected Builder page lock changed');end if;")
    return 'set define off\ndeclare n number;begin\n' + '\n'.join(statements) + '\nend;\n/\n'


from scripts.upgrade_apexlang import NativeBackend, quoted, literal


class NativePartialBackend(NativeBackend):
    def export(self, name):
        result = super().export(name)
        if name.startswith('live-'):
            self.live_application_bytes = (result / 'application.apx').read_bytes()
        return result

    def validate_source(self, source):
        import subprocess
        from scripts.sqlcl_session import bash_command
        from scripts.validate_app_source import validate_import_effects
        validate_import_effects(source)
        directory = self.base / 'compiler-validation'
        directory.mkdir(parents=True, mode=0o700)
        driver = directory / 'validate.sql'
        driver.write_text('set define off\nwhenever oserror exit failure\nwhenever sqlerror exit failure\napex validate -input ' + quoted(source) + ' -deployment ' + quoted(source / 'deployments/dev.json') + '\nexit success rollback\n', encoding='utf-8')
        stdin = directory / '.stdin'; stdin.touch(mode=0o600)
        with stdin.open('r') as stream:
            result = subprocess.run([bash_command(), '-c', 'source "$1"; invoke_sqlcl_safe "$2" -S -noupdates -nohistory /nolog "$3"', 'bash', str(self.repo_root / 'scripts/sqlcl_safe.sh'), str(directory), '@' + str(driver)], stdin=stream, capture_output=True, text=True, timeout=300)
        (directory / 'sqlcl-output.log').write_text(result.stdout + result.stderr, encoding='utf-8')
        if result.returncode or 'Validation successful' not in result.stdout or re.search(r'(?im)^\s*(?:ORA-|PLS-|SP2-|Error|Unknown Command)', result.stdout):
            raise ValueError('full merged-tree compilation failed; inspect private compiler diagnostics')

    def read_locks(self):
        import uuid
        directory, _ = self.execute('page-lock-read-' + uuid.uuid4().hex,
            f'set define on\ndefine app_id={self.app_id}\n@' + quoted(self.repo_root / 'scripts/page_lock_state.sql') + ' .apex-page-locks.json\nexit success rollback\n')
        from scripts.source_evidence import validate_page_locks
        return validate_page_locks(json.loads((directory / '.apex-page-locks.json').read_text(encoding='utf-8')), self.app_id)

    def import_pages(self, stage, selected, version, locks):
        from scripts.application_lock import assertion_sql
        current = selected_lock_rows(self.read_locks(), selected, self.app_id, self.workspace, self.evidence.developer)
        if current != locks:
            raise ValueError('selected Builder locks changed immediately before import')
        # Expected manifest is stamped for verification. The actual compiler input
        # keeps the fresh live manifest, so only the public version API writes it.
        actual = self.base / 'page-import-input'
        shutil.copytree(stage, actual)
        (actual / 'application.apx').write_bytes(self.live_application_bytes)
        actual_selected = tuple(actual / selected_relative(path) for path in selected)
        guard = assertion_sql(self.target, self.evidence) + page_lock_guard(self.app_id, actual_selected, locks)
        expected = literal(self.last_revision.version or '')
        revision_guard = f"declare v varchar2(255);begin select version into v from apex_applications where application_id={self.app_id};if nvl(v,chr(0))<>nvl({expected},chr(0)) then raise_application_error(-20085,'Live application version changed during partial preflight');end if;end;\n/\n"
        driver = build_partial_driver(actual_selected, actual / 'deployments/dev.json', self.app_id, version)
        command, update = driver.split('set define on\n', 1)
        body = guard + revision_guard + command + guard + revision_guard + 'set define on\n' + update + guard + f'prompt APEX_PARTIAL_IMPORT_VERIFIED:{self.app_id}\nexit success commit\n'
        _, result = self.execute('page-import', body, sentinel=f'APEX_PARTIAL_IMPORT_VERIFIED:{self.app_id}')
        if 'Import successful.' not in [line.strip() for line in result.output.splitlines()]:
            raise ValueError('selected import did not report success; baseline/native lock retained')


def main(argv=None):
    import argparse
    import os
    import subprocess
    import uuid
    from scripts.db_targets import Target
    from scripts.validate_app_source import validate_app_source
    from scripts.check_mirror_source import baseline_hash
    from scripts.sqlcl_session import bash_command
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app_id', type=int)
    parser.add_argument('--file', action='append', required=True)
    parser.add_argument('--source-dir', type=Path, required=True)
    parser.add_argument('--repo-root', type=Path, required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--connection', required=True)
    parser.add_argument('--schema', required=True)
    parser.add_argument('--expected-user', required=True)
    parser.add_argument('--classification', required=True)
    parser.add_argument('--developer', required=True)
    parser.add_argument('--developer-name', required=True)
    parser.add_argument('--no-team-notice', action='store_true', help='assert separate workspace accounts and the agreed guarded workflow; all ownership/cutoff checks still apply')
    args = parser.parse_args(argv)
    base = args.repo_root / 'scratch' / ('apex-partial.' + uuid.uuid4().hex)
    try:
        source = validate_app_source(args.repo_root, args.source_dir)
        original = tree_hashes(source)
        original_marker = baseline_hash(source)
        target = Target('dev', args.connection, args.expected_user, args.schema, args.classification)
        backend = NativePartialBackend(target, args.app_id, source, args.workspace, base, args.developer, args.repo_root)
        stage = publish_partial(source, args.app_id, args.file, args.workspace, base / 'publication', backend, args.developer_name, no_team_notice=args.no_team_notice)
        relative = source.relative_to(args.repo_root.resolve()).as_posix()
        receipt = base / 'verified-local-source.json'
        receipt.write_text(json.dumps({'schemaVersion': 1, 'relativeDirectory': relative, 'sourceFiles': original, 'baselineHash': original_marker}) + '\n', encoding='utf-8')
        if os.name == 'nt':
            engine = shutil.which('pwsh') or shutil.which('powershell')
            if not engine:
                raise ValueError('PowerShell is required to install canonical source on Windows')
            command = [engine, '-NoProfile', '-File', str(args.repo_root / 'scripts/replace_mirror.ps1'), '-VerifiedSource', str(receipt), str(stage), relative]
        else:
            command = [bash_command(), str(args.repo_root / 'scripts/replace_mirror.sh'), '--verified-source', str(receipt), str(stage), relative]
        result = subprocess.run(command, cwd=args.repo_root, capture_output=True, text=True)
        if result.returncode:
            raise ValueError('verified partial publication could not synchronize local source; retain canonical staging and reconcile newer edits')
        report_path = base / 'publication/partial.json'
        report = json.loads(report_path.read_text()); report['status'] = 'installed'
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(f'Partial DEV publish verified for app {args.app_id}; canonical source and baseline synchronized. Review selected edits and unrelated live updates. Report: {report_path}')
        if args.no_team_notice:
            print('Verified page ownership and explicit cutoff satisfy the requested no-team-notice policy under the asserted separate-account guarded workflow.')
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'partial publish error: {exc}; inspect retained recovery evidence: {base}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f'partial publish interrupted; original baseline/recovery evidence retained: {base}', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
