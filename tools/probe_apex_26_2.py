#!/usr/bin/env python3
"""Qualify APEX 26.2 on an explicitly identified disposable local target."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.apex_compatibility import parse_sqlcl_version, require_release, require_sqlcl_version
from scripts.db_targets import ORACLE_IDENTIFIER, SQLCL_ALIAS, Target, looks_like_production_identity
from scripts.sqlcl_session import SqlclError, bash_command, run_sqlcl

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT_ERRORS = re.compile(r"(?im)^\s*(?:Unknown Command\b|Error starting at line\b|Error report -|(?:ORA|PLS|SP2|TNS|SQL)-[0-9]+:)")


class ProbeError(ValueError):
    """Qualification failed; any disposable fixture remains for explicit recovery."""


def literal(value: str) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in "\0\r\n"):
        raise ProbeError("probe values must be nonempty single-line text")
    return "'" + value.replace("'", "''") + "'"


def command_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if any(c in value for c in '\0\r\n"'):
        raise ProbeError("SQLcl command paths cannot contain quotes or control characters")
    return '"' + value + '"'


@dataclass(frozen=True)
class ProbeTarget:
    connection: str
    expected_user: str
    schema: str
    workspace: str
    app_id: int
    server_host: str
    service: str

    def __post_init__(self):
        if not SQLCL_ALIAS.fullmatch(self.connection):
            raise ProbeError("use a saved SQLcl connection name")
        if not ORACLE_IDENTIFIER.fullmatch(self.expected_user) or not ORACLE_IDENTIFIER.fullmatch(self.schema):
            raise ProbeError("expected user and schema must be uppercase Oracle identifiers")
        if type(self.app_id) is not int or not 0 < self.app_id < 10**18:
            raise ProbeError("provide an unused positive numeric app ID")
        for value in (self.workspace, self.server_host, self.service):
            literal(value)
        if looks_like_production_identity(self.connection, self.server_host, self.service):
            raise ProbeError("this qualification probe is for a local development target")


def client_version(run_dir: Path) -> str:
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    result = subprocess.run([bash_command(), "-c",
                             'REPO_ROOT=$1; source "$1/scripts/sqlcl_safe.sh"; sqlcl_require_apex_version "$2"',
                             "apex-probe", str(REPO_ROOT), str(run_dir)],
                            capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise ProbeError("SQLcl compatibility unavailable; inspect the private version diagnostics")
    return parse_sqlcl_version((run_dir / "sqlcl-version.txt").read_text(encoding="utf-8"))


def tree_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ProbeError("fixture source cannot contain symbolic links")
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def require_version_changed_files(changed: set[str]):
    if changed not in ({"application.apx"}, {"application.apx", "deployments/default.json"}):
        raise ProbeError("unexpected version-stamp materialization")


class Probe:
    def __init__(self, target: ProbeTarget, report_dir: Path, *, runner=run_sqlcl, version_reader=client_version):
        self.target = target
        if report_dir.is_symlink() or (report_dir / "report.json").exists():
            raise ProbeError("use a fresh private report directory")
        self.directory = report_dir.resolve()
        self.runner = runner
        self.version_reader = version_reader
        self.run_id = uuid.uuid4().hex
        self.alias = f"APEX262-PROBE-{target.app_id}-{self.run_id[:12].upper()}"
        self.report = {"schemaVersion": 1, "runId": self.run_id, "allowWrites": False,
                       "appId": target.app_id, "fixtureAlias": self.alias, "checks": {}}

    def preamble(self) -> str:
        t = self.target
        return f'''set define off
set verify off
set echo off
set feedback off
set heading off
set pagesize 0
set linesize 32767
set serveroutput on size unlimited
set encoding UTF-8
whenever sqlerror exit failure rollback
whenever oserror exit failure rollback
declare v_group number;
begin
 if sys_context('USERENV','SESSION_USER') <> {literal(t.expected_user)}
 or sys_context('USERENV','CURRENT_SCHEMA') <> {literal(t.schema)}
 or sys_context('USERENV','SERVER_HOST') <> {literal(t.server_host)}
 or sys_context('USERENV','SERVICE_NAME') <> {literal(t.service)}
 then raise_application_error(-20080,'Wrong local qualification target'); end if;
 v_group := apex_util.find_security_group_id({literal(t.workspace)});
 if v_group is null then raise_application_error(-20081,'Workspace not found'); end if;
 apex_util.set_security_group_id(v_group);
 if nv('FLOW_SECURITY_GROUP_ID') <> v_group
 then raise_application_error(-20082,'Wrong APEX context'); end if;
end;
/
'''

    def owned_guard(self) -> str:
        t = self.target
        return f'''declare n number; begin
 select count(*) into n from apex_applications where application_id={t.app_id}
 and workspace={literal(t.workspace)} and owner={literal(t.schema)} and alias={literal(self.alias)};
 if n<>1 then raise_application_error(-20083,'Disposable application identity mismatch'); end if;
end;
/
'''

    def save_report(self):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = self.directory / "report.json.tmp"
        temporary.write_text(json.dumps(self.report, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.directory / "report.json")

    def execute(self, name: str, body: str, *, success_line: str | None = None) -> str:
        if re.fullmatch(r"[a-z0-9-]+", name) is None:
            raise ProbeError("invalid qualification phase")
        directory = self.directory / "runs" / name
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        driver = directory / "driver.sql"
        driver.write_text(self.preamble() + body + f"\nprompt APEX_PROBE_VERIFIED:{name}\nexit success rollback\n", encoding="utf-8")
        target = Target("dev", self.target.connection, self.target.expected_user, self.target.schema, "development")
        try:
            result = self.runner(target, driver, directory)
            output = result.output
            if CLIENT_ERRORS.search(output) or f"APEX_PROBE_VERIFIED:{name}" not in output.splitlines():
                raise ProbeError(f"{name} did not complete unambiguously")
            if success_line and success_line not in [line.strip() for line in output.splitlines()]:
                raise ProbeError(f"{name} did not report the native success line")
        except (SqlclError, ProbeError) as error:
            self.report["checks"][name] = {"status": "fail", "diagnostics": f"runs/{name}/sqlcl-output.log"}
            self.save_report()
            raise ProbeError(f"{name} failed; inspect private diagnostics; no automatic cleanup ran") from error
        self.report["checks"][name] = {"status": "pass"}
        self.save_report()
        return output

    def identify(self) -> dict:
        t = self.target
        body = f'''select 'APEX_PROBE_IDENTITY:' || json_object(
 'sessionUser' value sys_context('USERENV','SESSION_USER'),
 'currentSchema' value sys_context('USERENV','CURRENT_SCHEMA'),
 'serverHost' value sys_context('USERENV','SERVER_HOST'),
 'service' value sys_context('USERENV','SERVICE_NAME'),
 'workspace' value {literal(t.workspace)},
 'workspaceId' value apex_util.find_security_group_id({literal(t.workspace)}),
 'apexVersion' value (select version_no from apex_release),
 'databaseVersion' value (select version_full from product_component_version where product like 'Oracle%Database%'),
 'appCount' value (select count(*) from apex_applications where application_id={t.app_id})) from dual;
'''
        output = self.execute("identity", body)
        rows = [line.strip().removeprefix("APEX_PROBE_IDENTITY:") for line in output.splitlines()
                if line.strip().startswith("APEX_PROBE_IDENTITY:")]
        if len(rows) != 1:
            raise ProbeError("identity evidence is missing or ambiguous")
        identity = json.loads(rows[0])
        expected = {"sessionUser": t.expected_user, "currentSchema": t.schema, "serverHost": t.server_host,
                    "service": t.service, "workspace": t.workspace}
        if any(identity.get(key) != value for key, value in expected.items()) or not identity.get("workspaceId"):
            self.report["checks"]["identity"] = {"status": "fail"}
            raise ProbeError("reported target identity does not match the authorized target")
        require_release(identity.get("apexVersion", ""))
        self.report["identity"] = identity
        return identity

    def export(self, name: str) -> Path:
        parent = self.directory / "exports" / name
        parent.mkdir(parents=True, exist_ok=True)
        self.execute("export-" + name, self.owned_guard() +
                     f"apex export -applicationid {self.target.app_id} -exptype APEXLANG -dir {command_path(parent)}\n")
        apps = list(parent.rglob("application.apx"))
        if len(apps) != 1:
            raise ProbeError("native export did not produce exactly one application")
        return apps[0].parent

    def run(self, *, allow_writes=False, source_26_1: Path | None = None, developers: tuple[str, str] | None = None) -> dict:
        self.report["allowWrites"] = allow_writes
        try:
            version = self.version_reader(self.directory / "version")
            require_sqlcl_version(version)
            self.report["sqlclVersion"] = version
            identity = self.identify()
            if allow_writes:
                if identity.get("appCount") != 0:
                    raise ProbeError("requested disposable app ID already exists; nothing was imported")
                if source_26_1 is None or developers is None or len(developers) != 2 or developers[0].upper() == developers[1].upper():
                    raise ProbeError("write qualification needs authentic 26.1 source and two distinct existing workspace developers")
                self.qualify_writes(source_26_1, developers)
            else:
                self.report["checks"]["write-boundaries"] = {"status": "unavailable", "reason": "read-only mode"}
            self.save_report()
            return self.report
        except Exception:
            self.report["checks"]["completion"] = {"status": "fail"}
            self.save_report()
            raise

    def qualify_writes(self, source: Path, developers: tuple[str, str]):
        # Fail before the first write if the local fixture is not authentic 26.1.
        metadata = json.loads((source / ".apex/apexlang.json").read_text(encoding="utf-8"))
        if not str(metadata.get("mmdVersion", "")).startswith("26.1."):
            raise ProbeError("source must retain an authentic 26.1 format marker")
        input_hashes = tree_hashes(source)
        if any(path.read_text(encoding="utf-8").strip() for path in (source / "supporting-objects").rglob("*.sql")):
            raise ProbeError("use a fixture without executable supporting-object scripts")
        staged = self.directory / "input"
        shutil.copytree(source, staged)
        application = staged / "application.apx"
        text = application.read_text(encoding="utf-8")
        text, count = re.subn(r"(?m)^app [^\s]+ \(", "app " + self.alias + " (", text, count=1)
        if count != 1:
            raise ProbeError("source needs one application declaration")
        application.write_text(text, encoding="utf-8")
        descriptor = staged / "deployments/probe.json"
        descriptor.parent.mkdir(parents=True, exist_ok=True)
        descriptor.write_text(json.dumps({"workspace": {"name": self.target.workspace},
                                          "app": {"id": self.target.app_id, "databaseSession": {"parsingSchema": self.target.schema}}}) + "\n")
        t = self.target
        users = "\n".join(f'''select count(*) into n from apex_workspace_apex_users
 where workspace_name={literal(t.workspace)} and upper(user_name)=upper({literal(user)})
 and (is_admin='Yes' or is_application_developer='Yes');
 if n<>1 then raise_application_error(-20084,'Expected existing workspace developer'); end if;''' for user in developers)
        guard = f'''declare n number; begin
 {users}
 select count(*) into n from apex_applications where application_id={t.app_id};
 if n<>0 then raise_application_error(-20085,'Disposable app ID is occupied'); end if;
end;
/
'''
        self.report["fixtureWriteAttempted"] = True
        self.report["cleanupRequired"] = True
        self.report["developers"] = list(developers)
        self.save_report()
        self.execute("fixture-import", guard + f"apex import -input {command_path(staged)} -deployment {command_path(descriptor)}\n", success_line="Import successful.")
        first, second = self.export("converted"), self.export("converted-repeat")
        if tree_hashes(first) != tree_hashes(second):
            raise ProbeError("canonical exports are not stable")
        version = json.loads((first / ".apex/apexlang.json").read_text())["mmdVersion"]
        if not version.startswith("26.2."):
            raise ProbeError("server did not convert the source to 26.2")
        for relative, digest in input_hashes.items():
            if relative.lower().endswith((".png", ".jpg", ".gif", ".woff", ".woff2")):
                if tree_hashes(first).get(relative) != digest:
                    raise ProbeError("binary fixture asset changed")
        self.report["checks"]["canonical-conversion"] = {"status": "pass", "format": version,
                                                         "fileCount": len(tree_hashes(first))}
        owner, other = developers
        comment = "apex262-probe:" + self.run_id
        self.execute("lock-commit", self.owned_guard() +
                     f"begin apex_application_admin.lock_application({t.app_id},{literal(owner)},{literal(comment)}); commit; end;\n/\n")
        self.verify_lock("lock-persisted", owner, comment)
        rejection = f'''begin
 begin apex_application_admin.lock_application({t.app_id},{literal(other)},'conflict');
 raise_application_error(-20086,'Different owner was accepted');
 exception when others then if sqlcode<>-20001 then raise; end if; end;
 begin apex_application_admin.unlock_application({t.app_id},{literal(other)});
 raise_application_error(-20086,'Wrong-owner release was accepted');
 exception when others then if sqlcode<>-20001 then raise; end if; end;
end;
/
'''
        self.execute("owner-refusals", self.owned_guard() + rejection)
        self.execute("owner-reentry", self.owned_guard() +
                     f"begin apex_application_admin.lock_application({t.app_id},{literal(owner)},{literal(comment)}); commit; end;\n/\n")
        self.verify_lock("owner-reentry-persisted", owner, comment)
        self.report["checks"]["same-owner-process-mutex"] = {"status": "unavailable", "reason": "native API permits owner reentry"}
        self.execute("first-owner-release", self.owned_guard() +
                     f"begin apex_application_admin.unlock_application({t.app_id},{literal(owner)}); commit; end;\n/\n")
        self.execute("other-owner-lock", self.owned_guard() +
                     f"begin apex_application_admin.lock_application({t.app_id},{literal(other)},{literal(comment)}); commit; end;\n/\n")
        self.execute("raw-import-held-lock", self.owned_guard() +
                     f"apex import -input {command_path(staged)} -deployment {command_path(descriptor)}\n", success_line="Import successful.")
        self.verify_lock("import-lock-preserved", other, comment)
        current = self.export("after-raw-held")
        if tree_hashes(current) != tree_hashes(first):
            raise ProbeError("unchanged full import altered canonical source")
        self.qualify_partial(current, descriptor, other, comment)
        current = self.export("after-version")
        self.qualify_descriptor(current, descriptor, other, comment)
        self.execute("owner-release", self.owned_guard() +
                     f"begin apex_application_admin.unlock_application({t.app_id},{literal(other)}); commit; end;\n/\n")
        self.report["checks"]["runtime-bookmark-cutoff"] = {"status": "unavailable", "reason": "requires a separate Builder/runtime qualification"}
        self.report["cleanupRequired"] = True
        self.report["developers"] = list(developers)

    def qualify_partial(self, canonical: Path, descriptor: Path, owner: str, comment: str):
        staged = self.directory / "partial-input"
        shutil.copytree(canonical, staged)
        selected = staged / "pages/p00001-home.apx"
        unselected = staged / "pages/p09999-login.apx"
        if not selected.is_file() or not unselected.is_file():
            raise ProbeError("page qualification requires the example's home and login pages")
        page = selected.read_text(encoding="utf-8")
        if "    title: Home\n" not in page:
            raise ProbeError("unexpected fixture home page title")
        selected.write_text(page.replace("    title: Home\n", "    title: Qualification Home\n", 1), encoding="utf-8")
        page = unselected.read_text(encoding="utf-8")
        unselected.write_text(page.replace("    name: Login Page\n", "    name: Unselected Dirty Login\n", 1), encoding="utf-8")
        self.execute("raw-page-import-held-lock", self.owned_guard() +
                     f"apex import -input {command_path(staged)} -deployment {command_path(descriptor)} -files {command_path(selected)}\n",
                     success_line="Import successful.")
        self.verify_lock("page-import-lock-preserved", owner, comment)
        result = self.export("page-selected")
        expected = tree_hashes(canonical)
        expected[selected.relative_to(staged).as_posix()] = hashlib.sha256(selected.read_bytes()).hexdigest()
        if tree_hashes(result) != expected:
            raise ProbeError("partial native import changed more than the selected page or failed to apply it")
        self.report["checks"]["native-page-selection"] = {"status": "pass"}
        version = f"Qualification [{self.run_id[:12]}]"
        self.execute("version-stamp", self.owned_guard() +
                     f"begin apex_application_admin.set_application_version({self.target.app_id},{literal(version)}); commit; end;\n/\n")
        stamped = self.export("version-stamped")
        old, new = tree_hashes(result), tree_hashes(stamped)
        changed = {path for path in old.keys() | new.keys() if old.get(path) != new.get(path)}
        require_version_changed_files(changed)
        strip_version = lambda text: re.sub(r"(?m)^    version:[^\n]*\n", "", text)
        if strip_version((result / "application.apx").read_text()) != strip_version((stamped / "application.apx").read_text()):
            raise ProbeError("version stamp changed unrelated application source")
        before_descriptor = json.loads((result / "deployments/default.json").read_text())
        after_descriptor = json.loads((stamped / "deployments/default.json").read_text())
        for deployment in (before_descriptor, after_descriptor):
            deployment.get("app", {}).get("sessionStateProtection", {}).pop("allowUrlsCreatedAfter", None)
        if before_descriptor != after_descriptor:
            raise ProbeError("version stamp changed unrelated deployment values")
        if tree_hashes(stamped) != tree_hashes(self.export("version-stamped-repeat")):
            raise ProbeError("version-stamp export is unstable")
        self.verify_lock("version-lock-preserved", owner, comment)
        self.report["checks"]["version-export-effects"] = {"status": "pass", "changedFiles": sorted(changed)}

    def qualify_descriptor(self, canonical: Path, descriptor: Path, owner: str, comment: str):
        import secrets
        selected = json.loads(descriptor.read_text())
        salt = secrets.token_hex(32).upper()
        name = "APEX262 Qualification Override"
        selected["app"].update({"name": name, "runtime": {"debugging": True, "logging": False},
                                "sessionStateProtection": {"checksumSalt": salt}})
        effective = self.directory / "selected-descriptor.json"
        effective.write_text(json.dumps(selected) + "\n", encoding="utf-8")
        staged = self.directory / "deployment-input"
        shutil.copytree(canonical, staged)
        conflicting = {"app": {"name": "Ignored Default Override", "runtime": {"debugging": False, "logging": True},
                               "sessionStateProtection": {"checksumSalt": "0" * 64}}}
        (staged / "deployments/default.json").write_text(json.dumps(conflicting), encoding="utf-8")
        t = self.target
        self.execute("selected-deployment", self.owned_guard() +
                     f"apex import -input {command_path(staged)} -deployment {command_path(effective)}\n" + f'''declare n number; begin
 select count(*) into n from apex_applications where application_id={t.app_id}
 and application_name={literal(name)} and debugging='Allowed' and logging='No';
 if n<>1 then raise_application_error(-20088,'Selected deployment values did not materialize'); end if;
end;
/
''', success_line="Import successful.")
        self.verify_lock("deployment-lock-preserved", owner, comment)
        result = self.export("selected-deployment")
        generated = json.loads((result / "deployments/default.json").read_text())
        if generated.get("app", {}).get("sessionStateProtection", {}).get("checksumSalt", "").upper() != salt:
            raise ProbeError("selected deployment salt did not materialize")
        old, new = tree_hashes(canonical), tree_hashes(result)
        changed = {path for path in old.keys() | new.keys() if old.get(path) != new.get(path)}
        if changed != {"application.apx", "deployments/default.json"}:
            raise ProbeError("selected deployment changed unexpected source")
        self.report["checks"]["explicit-deployment"] = {"status": "pass", "saltVerified": True,
                                                        "defaultOverlay": False, "changedFiles": sorted(changed)}

    def verify_lock(self, name: str, owner: str, comment: str):
        self.execute(name, self.owned_guard() + f'''declare n number; begin
 select count(*) into n from apex_applications where application_id={self.target.app_id}
 and upper(locked_by)=upper({literal(owner)}) and lock_comment={literal(comment)};
 if n<>1 then raise_application_error(-20087,'Native lock was not preserved'); end if;
end;
/
''')

    def cleanup(self, previous_report: Path) -> dict:
        saved = json.loads(previous_report.read_text(encoding="utf-8"))
        t = self.target
        expected = {"sessionUser": t.expected_user, "currentSchema": t.schema, "serverHost": t.server_host,
                    "service": t.service, "workspace": t.workspace}
        run_id = saved.get("runId", "")
        developers = saved.get("developers", [])
        if (saved.get("schemaVersion") != 1 or saved.get("appId") != t.app_id
                or not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None
                or saved.get("fixtureAlias") != f"APEX262-PROBE-{t.app_id}-{run_id[:12].upper()}"
                or any(saved.get("identity", {}).get(key) != value for key, value in expected.items())
                or not isinstance(developers, list) or len(developers) != 2):
            raise ProbeError("cleanup report does not identify this exact disposable target")
        self.alias = saved["fixtureAlias"]
        self.report["fixtureAlias"] = self.alias
        self.report["allowWrites"] = True
        self.report["cleanupOfRunId"] = run_id
        require_sqlcl_version(self.version_reader(self.directory / "version"))
        self.identify()
        accepted_users = ",".join("upper(" + literal(user) + ")" for user in developers)
        self.execute("explicit-cleanup", self.owned_guard() + f'''declare
 v_owner varchar2(255); v_comment varchar2(4000); n number;
begin
 select locked_by,lock_comment into v_owner,v_comment from apex_applications where application_id={t.app_id};
 if v_owner is not null then
  if upper(v_owner) not in ({accepted_users}) or v_comment is null or v_comment<>{literal('apex262-probe:' + run_id)}
  then raise_application_error(-20089,'Fixture lock belongs to another operation; coordinate recovery'); end if;
  apex_application_admin.unlock_application({t.app_id},v_owner);
 end if;
 apex_application_install.remove_application({t.app_id});
 commit;
 select count(*) into n from apex_applications where application_id={t.app_id};
 if n<>0 then raise_application_error(-20090,'Fixture cleanup is unverified'); end if;
end;
/
''')
        self.report["cleanupRequired"] = False
        self.save_report()
        return self.report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("connection", "expected-user", "schema", "workspace", "server-host", "service"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--app-id", type=int, required=True)
    parser.add_argument("--allow-writes", action="store_true", help="only after human authorization of this exact disposable target")
    parser.add_argument("--source-26-1", type=Path)
    parser.add_argument("--developers", nargs=2)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--cleanup-report", type=Path, help="explicitly remove only the recorded disposable fixture; requires --allow-writes")
    args = parser.parse_args(argv)
    try:
        target = ProbeTarget(args.connection, args.expected_user, args.schema, args.workspace,
                             args.app_id, args.server_host, args.service)
        directory = args.report_dir or REPO_ROOT / ".sync-state/apex-26.2-probe" / uuid.uuid4().hex
        probe = Probe(target, directory)
        if args.cleanup_report:
            if not args.allow_writes:
                raise ProbeError("cleanup is a write; supply --allow-writes only after exact-target authorization")
            report = probe.cleanup(args.cleanup_report)
        else:
            report = probe.run(allow_writes=args.allow_writes, source_26_1=args.source_26_1,
                               developers=tuple(args.developers) if args.developers else None)
        print(json.dumps(report, indent=2))
        return 0
    except (ProbeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"APEX qualification failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
