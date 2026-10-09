#!/usr/bin/env python3
"""Stage an explicit, stable canonical APEXlang conversion for review/install."""
from __future__ import annotations

import json
import argparse
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.source_evidence import content_evidence, tree_hashes, read_page_lock_evidence
from scripts.db_targets import Target
from scripts.sqlcl_session import run_sqlcl, bash_command
from scripts.application_lock import acquire_lock, check_lock, release_lock, assertion_sql, require_client
from scripts.record_export_state import read_state, marker_payload
from scripts.deployment_descriptor import read_descriptor, observe_deployment
from scripts.validate_app_source import validate_app_source, validate_import_effects


def literal(value: str) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ValueError("conversion identity contains invalid text")
    return "'" + value.replace("'", "''") + "'"


def quoted(path: Path) -> str:
    value = path.resolve().as_posix()
    if any(char in value for char in '\r\n"&'):
        raise ValueError("conversion path cannot be passed to SQLcl")
    return '"' + value + '"'


class NativeBackend:
    """Public native operations with the existing isolated SQLcl/lock contracts."""
    def __init__(self, target, app_id, source, workspace, base, developer, repo_root):
        self.target, self.app_id, self.source = target, app_id, source
        self.workspace, self.base, self.developer, self.repo_root = workspace, base, developer, repo_root
        self.evidence = None
        self.approved = None
        self.last_revision = None
        self.last_observation = None
        self.descriptor = read_descriptor(source / "deployments/dev.json", app_id)
        if self.descriptor["workspace"]["name"] != workspace or self.descriptor["app"]["databaseSession"]["parsingSchema"] != target.schema:
            raise ValueError("conversion descriptor identity differs from the selected target")
        if target.environment != "dev" or target.classification not in {"dev", "development", "test"}:
            raise ValueError("source conversion targets DEV only")

    def require_clean(self, source):
        validate_app_source(self.repo_root, source)
        relative = source.resolve().relative_to(self.repo_root.resolve()).as_posix()
        result = subprocess.run(["git", "-C", str(self.repo_root), "status", "--porcelain", "--untracked-files=all", "--", ":(literal)" + relative], capture_output=True, text=True)
        if result.returncode or result.stdout.strip():
            raise ValueError("source conversion requires a clean source tree; commit or stash edits first")
        ignored = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(self.repo_root), "status", "--porcelain", "--ignored", "--untracked-files=all", "--", ":(literal)" + relative], capture_output=True, text=True)
        allowed = {relative + "/apex-team-export.json", relative + "/deployments/default.json"}
        if ignored.returncode or any(line.startswith("!! ") and line[3:] not in allowed for line in ignored.stdout.splitlines()):
            raise ValueError("source conversion refuses ignored local files that replacement would delete")

    def context(self):
        t = self.target
        return f'''set define off
set verify off
set echo off
set heading off
set feedback off
set pages 0
set lines 32767
whenever sqlerror exit failure rollback
whenever oserror exit failure rollback
declare n number; g number; v varchar2(100);begin
if sys_context('USERENV','SESSION_USER')<>{literal(t.expected_user)} then raise_application_error(-20080,'Wrong conversion session');end if;
if regexp_like(regexp_replace(sys_context('USERENV','DB_NAME') || ' ' ||
       sys_context('USERENV','DB_UNIQUE_NAME') || ' ' || sys_context('USERENV','SERVICE_NAME'),
       '(pre|non)[-_.]?(prod|prd)', ' ', 1, 0, 'i'),
       '(^|[^[:alnum:]])(production|live)[[:digit:]]*([^[:alnum:]]|$)|(prod|prd)[[:digit:]]*([^[:alnum:]]|$)|(^|[^[:alnum:]])(prod|prd)(db|[[:digit:]])', 'i') then
  raise_application_error(-20080,'Conversion is DEV only');end if;
select version_no into v from apex_release;
if not regexp_like(v,'^26[.]2([.]|$)') then raise_application_error(-20080,'Conversion requires APEX26.2');end if;
g:=apex_util.find_security_group_id({literal(self.workspace)});
if g is null then raise_application_error(-20080,'Conversion workspace unavailable');end if;
apex_util.set_security_group_id(g);
select count(*) into n from apex_applications where application_id={self.app_id} and workspace={literal(self.workspace)} and owner={literal(t.schema)} and is_working_copy='No';
if n<>1 then raise_application_error(-20080,'Conversion requires the selected existing main app');end if;
end;
/
'''

    def execute(self, name, body, *, sentinel=None, export_schema=None):
        directory = self.base / "runs" / name
        directory.mkdir(parents=True, mode=0o700)
        if export_schema is not None:
            (directory / "apps" / export_schema).mkdir(parents=True)
        driver = directory / "driver.sql"
        driver.write_text(self.context() + body, encoding="utf-8")
        result = run_sqlcl(self.target, driver, directory)
        if re.search(r"(?im)^\s*(?:Unknown Command|Error starting at line|Error report -|(?:ORA|PLS|SP2|TNS|SQL)-\d+:)", result.output):
            raise ValueError(f"conversion SQLcl phase failed; inspect private diagnostics: {directory}")
        if sentinel is not None and sentinel not in [line.strip() for line in result.output.splitlines()]:
            raise ValueError(f"conversion SQLcl result unavailable; inspect private diagnostics: {directory}")
        return directory, result

    def validate_target(self):
        require_client(self.base / "client-version")
        self.execute("target", "prompt APEX_CONVERSION_TARGET_VERIFIED\nexit success rollback\n", sentinel="APEX_CONVERSION_TARGET_VERIFIED")

    def validate_files_input(self, source):
        metadata = json.loads((source / '.apex/apexlang.json').read_text(encoding='utf-8'))
        version = metadata.get('mmdVersion') if isinstance(metadata, dict) else None
        if not isinstance(version, str) or re.fullmatch(r'26\.[12]\.\d+\+\d+', version) is None:
            raise ValueError('files conversion requires authentic APEX 26.1 or 26.2 source metadata')
        if version.startswith('26.1.'):
            # The real 26.1 import ignored a selected 26.2 checksum-salt override.
            # Refuse that unqualified combination before acquiring a native lock.
            if set(self.descriptor) - {'workspace', 'app'} or set(self.descriptor['app']) - {'id', 'databaseSession'}:
                raise ValueError('APEX 26.1 files conversion requires an identity-only descriptor; use Builder conversion before applying APEX 26.2 deployment overrides')

    def acquire(self):
        if not self.developer:
            raise ValueError("files conversion requires explicit APEX_WORKSPACE_USERNAME")
        self.evidence = acquire_lock(self.target, self.app_id, self.workspace, self.developer, self.base / "lock")

    def check(self):
        check_lock(self.target, self.evidence, self.base / "lock")

    def release(self):
        release_lock(self.target, self.evidence, self.base / "lock")

    def drift(self, source):
        token_file = self.base / "approved-live-state.txt"
        result = subprocess.run([sys.executable, str(self.repo_root / "scripts/check_builder_drift.py"), str(self.app_id), self.target.connection, str(source), "--expected-user", self.target.expected_user, "--state-out", str(token_file)], capture_output=True, text=True)
        if result.returncode:
            raise ValueError("files conversion refused Builder drift; export and reconcile first")
        self.approved = token_file.read_text().strip()
        if re.fullmatch(r"P\.([0-9T:-]+|NONE)\.[0-9A-F]*", self.approved) is None:
            raise ValueError("files conversion did not receive a verified live revision")

    def import_source(self, source):
        stamp = subprocess.run([sys.executable, str(self.repo_root / "scripts/stamp_publish_version.py"), str(source / "application.apx"), os.environ.get("DEVELOPER_NAME", "")], capture_output=True, text=True)
        if stamp.returncode:
            raise ValueError("conversion could not stamp its private import source")
        assertion = self.base / "assert-lock.sql"
        assertion.write_text(assertion_sql(self.target, self.evidence), encoding="utf-8")
        t = self.target
        body = "set define on\n@" + quoted(self.repo_root / "scripts/publish_app.sql") + " " + " ".join((t.schema, t.classification, t.expected_user, quoted(source), quoted(source / "deployments/dev.json"), str(self.app_id), self.approved, quoted(assertion))) + "\n"
        _, result = self.execute("import", body, sentinel=f"APEX_IMPORT_VERIFIED:{self.app_id}")
        if "Import successful." not in [line.strip() for line in result.output.splitlines()]:
            raise ValueError("files conversion import result unknown; native lock and original baseline retained")

    def export(self, name):
        t = self.target
        body = "set define on\n@" + quoted(self.repo_root / "scripts/export_apps.sql") + f" {t.schema} {self.app_id} {t.classification} {t.expected_user}\n"
        directory, _ = self.execute(name, body, export_schema=t.schema)
        parent = directory / "apps" / t.schema
        exports = list(parent.iterdir()) if parent.is_dir() else []
        if len(exports) != 1 or not exports[0].is_dir():
            raise ValueError("conversion did not produce one complete native export")
        before, first_at = read_state(directory / ".apex-export-before.txt")
        after, second_at = read_state(directory / ".apex-export-after.txt")
        if not before.present or before != after or first_at > second_at or before.last_updated_on in {first_at.isoformat(timespec="seconds"), second_at.isoformat(timespec="seconds")}:
            raise ValueError("conversion export revision was unstable or ambiguous")
        self.last_revision = after
        self.last_observation = directory / ".apex-deployment-state.json"
        self.last_page_locks = read_page_lock_evidence(directory / '.apex-page-locks-before.json', directory / '.apex-page-locks-after.json', self.app_id)
        return exports[0]

    def verify_effective(self, source, exported):
        observe_deployment(self.last_observation, exported / "deployments/default.json", self.descriptor)


def normalize_source(root: Path) -> None:
    tree_hashes(root)  # Refuse links before modifying the private exported tree.
    for path in root.rglob("*.apx"):
        raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        path.write_bytes(raw.rstrip(b"\n") + b"\n")


def preserve_authored(existing: Path, staged: Path) -> None:
    for name in ("dev.json", "staging.json", "prod.json"):
        source = existing / "deployments" / name
        if source.is_symlink():
            raise ValueError("authored deployment descriptor must not be a symbolic link")
        if source.is_file():
            destination = staged / "deployments" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


def upgrade_source(target, app_id: int, source_dir: Path, workspace: str,
                   mode: str, run_dir: Path, *, backend=None) -> Path:
    """Return verified staged source; originals and their baseline stay untouched.

    CLI installation uses the existing mirror replacement primitive. A backend
    owns target/identity/drift checks and native lock/import/export operations.
    An unverified attempted import retains its native lock and recovery proof.
    """
    if mode not in {"builder", "files"}:
        raise ValueError("upgrade mode must be builder or files")
    if type(app_id) is not int or not 0 < app_id < 10**18:
        raise ValueError("upgrade requires a positive numeric application ID")
    if backend is None:
        if target is None:
            raise ValueError("native conversion target is required")
        backend = NativeBackend(target, app_id, source_dir, workspace, run_dir.parent,
                                os.environ.get("APEX_WORKSPACE_USERNAME", ""), source_dir.resolve().parents[2])
    if run_dir.exists() or run_dir.is_symlink():
        raise ValueError("conversion requires a fresh private run directory")
    backend.require_clean(source_dir)
    original = tree_hashes(source_dir)
    if mode == 'files':
        validate_import_effects(source_dir)
        if hasattr(backend, 'validate_files_input'):
            backend.validate_files_input(source_dir)
    run_dir.mkdir(parents=True, mode=0o700)
    shutil.copytree(source_dir, run_dir / "source-before")
    report = {"applicationId": app_id, "workspace": workspace, "mode": mode,
              "status": "started", "importAttempted": False, "lockRetained": False}
    try:
        old_format = json.loads((source_dir / ".apex/apexlang.json").read_text(encoding="utf-8")).get("mmdVersion")
        report["oldMmdVersion"] = old_format if isinstance(old_format, str) and re.fullmatch(r"\d+\.\d+\.\d+\+\d+", old_format) else None
    except (OSError, ValueError, AttributeError):
        report["oldMmdVersion"] = None
    held = False
    try:
        backend.validate_target()
        if mode == "files":
            backend.acquire()
            held = True
            backend.drift(source_dir)
            live = backend.export("live-before")
            normalize_source(live)
            shutil.copytree(live, run_dir / "live-before")
            if tree_hashes(source_dir) != original:
                raise ValueError("local source changed during conversion preflight")
            # Import a backup copy; the working source is never stamped/modified.
            staged_input = run_dir / "import-input"
            shutil.copytree(source_dir, staged_input)
            report["importAttempted"] = True
            backend.import_source(staged_input)
        first = backend.export("canonical-first")
        first_revision = getattr(backend, "last_revision", None)
        second = backend.export("canonical-second")
        if first_revision is not None and first_revision != backend.last_revision:
            raise ValueError("canonical database revision was not stable across exports")
        normalize_source(first)
        normalize_source(second)
        first_hashes, second_hashes = tree_hashes(first), tree_hashes(second)
        content_evidence(first)
        content_evidence(second)
        if first_hashes != second_hashes:
            raise ValueError("canonical export was not stable across two observations")
        if mode == "files":
            # Layout changes are intentional; imported static bytes must survive.
            for name, digest in original.items():
                if name.startswith(("static-files/", "shared-components/static-files/", "generated-artifacts/")) and second_hashes.get(name) != digest:
                    kind = "generated artifact" if name.startswith("generated-artifacts/") else "static source"
                    raise ValueError(f"conversion did not preserve {kind}: {name}")
            if isinstance(report['oldMmdVersion'], str) and report['oldMmdVersion'].startswith('26.2.'):
                from scripts.verify_publish_state import verify_source_bytes
                verify_source_bytes(staged_input, second, getattr(backend, 'descriptor', None))
            backend.verify_effective(source_dir, second)
        if tree_hashes(source_dir) != original:
            raise ValueError("local source changed during conversion; original retained")
        stage = run_dir / "canonical"
        shutil.copytree(second, stage)
        preserve_authored(source_dir, stage)
        if held:
            backend.check()
            backend.release()
            held = False
        canonical = tree_hashes(stage)
        report.update({"status": "staged", "sourceFormat": content_evidence(stage)["sourceFormat"],
                       "changedFiles": sorted(name for name in original.keys() | canonical.keys()
                                              if original.get(name) != canonical.get(name)),
                       "sourceFiles": canonical})
        return stage
    except BaseException:
        report["status"] = "failed"
        if held and not report["importAttempted"]:
            try:
                backend.check()
                backend.release()
                held = False
            except BaseException:
                report["lockReleaseFailed"] = True
        report["lockRetained"] = held
        raise
    finally:
        (run_dir / "conversion.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_id", type=int)
    parser.add_argument("--mode", choices=("builder", "files"), required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--connection", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--expected-user", required=True)
    parser.add_argument("--classification", required=True)
    parser.add_argument("--developer", default="")
    args = parser.parse_args(argv)
    base = args.repo_root / "scratch" / ("apexlang-upgrade." + uuid.uuid4().hex)
    try:
        target = Target("dev", args.connection, args.expected_user, args.schema, args.classification)
        source = validate_app_source(args.repo_root, args.source_dir)
        backend = NativeBackend(target, args.app_id, source, args.workspace, base, args.developer, args.repo_root)
        stage = upgrade_source(target, args.app_id, source, args.workspace, args.mode, base / "conversion", backend=backend)
        marker = marker_payload(args.app_id, backend.last_revision, stage, backend.last_page_locks)
        (stage / "apex-team-export.json").write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        relative = source.relative_to(args.repo_root.resolve()).as_posix()
        if os.name == "nt":
            engine = shutil.which("pwsh") or shutil.which("powershell")
            if engine is None:
                raise ValueError("PowerShell is required for atomic source installation on Windows")
            command = [engine, "-NoProfile", "-File", str(args.repo_root / "scripts/replace_mirror.ps1"), str(stage), relative]
        else:
            command = [bash_command(), str(args.repo_root / "scripts/replace_mirror.sh"), str(stage), relative]
        result = subprocess.run(command, cwd=args.repo_root, capture_output=True, text=True)
        if result.returncode:
            raise ValueError("verified canonical source could not be installed; original source and evidence retained")
        report_path = base / "conversion/conversion.json"
        report = json.loads(report_path.read_text()); report["status"] = "installed"
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Canonical APEX 26.2 source installed for app {args.app_id}. Review the structural diff. Report: {report_path}")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"source upgrade error: {exc}; recovery directory: {base}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
