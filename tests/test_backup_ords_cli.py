"""backup-ords, and ORDS inside backup-db and doctor, through team.sh and team.ps1.

SQLcl is a scriptable fake (fake_ords_sql.py): each test chooses what it
returns, and asserts what the wrappers installed, kept, refused and asked SQLcl
to do. Every scenario runs through both wrappers where PowerShell Core exists.
No database is involved.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
import fake_sqlcl
from fake_sqlcl import BASH
from ords_fixtures import SCHEMA, export_text

from scripts import ords_export

ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
FAKE = Path(__file__).resolve().parent / "fake_ords_sql.py"

SQL_FILES = (
    "backup_db.sql", "doctor.sql", "doctor_ords.sql", "verify_db_access.sql", "verify_ords_access.sql",
    "ords_export.sql", "ords_export_run.sql", "ords_export_skip.sql", "ords_inventory.sql", "ords_export.py", "apex_compatibility.py",
)
BASH_FILES = ("team.sh", "backup_db.sh", "load_env.sh", "check_db_target.sh", "sqlcl_safe.sh", "replace_mirror.sh")
POWERSHELL_FILES = (
    "team.ps1", "backup_db.ps1", "load_env.ps1", "check_db_target.ps1", "invoke_sqlcl.ps1", "resolve_python.ps1", "replace_mirror.ps1",
)

ORDS_REST = "\nORDS_SCHEMA=REST_API\nORDS_SQLCL_CONNECTION=dev-rest\nORDS_EXPECTED_USER=REST_API\n"
ORDS_SAME = "\nORDS_SCHEMA=DEMO\nORDS_SQLCL_CONNECTION=docker-demo\nORDS_EXPECTED_USER=DEMO\n"
ORDS_TWO = "\nORDS_SCHEMA=REST_ONE,REST_TWO\nORDS_SQLCL_CONNECTION=dev-one,dev-two\nORDS_EXPECTED_USER=REST_ONE,REST_TWO\n"

SEED = {
    "database/DEMO/tables/OLD_TABLE.sql": "old table\n",
    "database/DEMO/views/OLD_VIEW.sql": "old view\n",
    "database/DEMO/manifest-tables.txt": "TABLE=1\n",
    "database/DEMO/ords/schema.sql": "-- old ords for DEMO\n",
    "database/REST_API/ords/schema.sql": "-- old ords for REST_API\n",
    "database/REST_API/tables/KEEP_TABLE.sql": "keep this\n",
    "database/REST_ONE/ords/schema.sql": "-- old ords for REST_ONE\n",
    "database/REST_TWO/ords/schema.sql": "-- old ords for REST_TWO\n",
}


def plain(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return re.sub(r"\s*\n\s*\|?\s*", " ", text)


def shells() -> list[str]:
    return ["bash", "powershell"] if PWSH else ["bash"]


def tree(root: Path) -> dict[str, bytes]:
    base = root / "database"
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(base.rglob("*")) if path.is_file()}


class Project:
    """A throwaway checkout with the template's scripts, a committed seed mirror and the fake SQLcl."""

    def __init__(self, root: Path, shell: str, ords: str = ORDS_REST, config: dict | None = None, seed: dict | None = None, env_extra: str = "") -> None:
        self.root = root
        self.shell = shell
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        for name in (BASH_FILES if shell == "bash" else POWERSHELL_FILES) + SQL_FILES:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8") + ords + env_extra, encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Ords Test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "ords@example.test"], check=True)
        (root / ".gitignore").write_text("scratch/\n.env\nbin/\ncalls.txt\nconfig.json\nstarted\n", encoding="utf-8")
        for relative, contents in (SEED if seed is None else seed).items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8", newline="")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)
        self.calls = root / "calls.txt"
        self.config_path = root / "config.json"
        self.configure(config or {})
        fake_bin = root / "bin"
        fake_sqlcl.install(fake_bin, f"exec '{Path(sys.executable).as_posix()}' '{FAKE.as_posix()}' \"$@\"\n")
        self.environment = os.environ.copy()
        self.environment["PATH"] = f"{fake_bin}{os.pathsep}{self.environment['PATH']}"
        self.environment["PROJECT_ENV_FILE"] = str(root / ".env")
        self.environment["FAKE_ORDS_CONFIG"] = str(self.config_path)
        self.environment.pop("PROJECT_SCHEMA", None)

    def configure(self, config: dict) -> None:
        self.config_path.write_text(json.dumps({"calls": str(self.calls), **config}), encoding="utf-8")

    def command(self, *arguments: str) -> list[str]:
        if self.shell == "bash":
            return [BASH, str(self.root / "scripts" / "team.sh"), *arguments]
        return [PWSH, "-NoProfile", "-File", str(self.root / "scripts" / "team.ps1"), *arguments]

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(self.command(*arguments), cwd=self.root, env=self.environment, text=True, capture_output=True, check=False)

    def logged(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def leftovers(self) -> list[str]:
        scratch = self.root / "scratch"
        if not scratch.exists():
            return []
        return sorted(path.name for path in scratch.iterdir() if path.name.startswith(("db-backup", ".mirror-backup")))


class OrdsCliTestCase(unittest.TestCase):
    def each_shell(self, body, **project_arguments) -> None:
        for shell in shells():
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temporary:
                body(Project(Path(temporary), shell, **project_arguments))

    def installed_export(self, schema: str = "REST_API", **counts: int) -> bytes:
        return ords_export.normalize(export_text(schema, counts or None)).encode("utf-8")


class BackupOrdsTests(OrdsCliTestCase):
    def test_backup_ords_installs_the_verified_export_and_leaves_table_and_code_mirrors_alone(self) -> None:
        def body(project: Project) -> None:
            before = tree(project.root)
            result = project.run("backup-ords")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            after = tree(project.root)
            self.assertEqual(self.installed_export(), after["database/REST_API/ords/schema.sql"])
            self.assertNotIn(b"\r", after["database/REST_API/ords/schema.sql"])
            self.assertNotIn(b"Date:", after["database/REST_API/ords/schema.sql"])
            changed = {path for path in after if after[path] != before.get(path)}
            self.assertEqual({"database/REST_API/ords/schema.sql"}, changed, "only the ORDS export may change")
            self.assertEqual(set(before), set(after), "no mirror file may appear or vanish")
            self.assertIn("ORDS export verified for REST_API", plain(result.stdout))
            self.assertNotIn("LONG setting", result.stdout)
            self.assertEqual(["version", "ords|REST_API|REST_API|dev-rest|ok"], project.logged())
            self.assertEqual([], project.leftovers())
        self.each_shell(body)

    def test_backup_ords_runs_nothing_for_table_or_code_profiles(self) -> None:
        def body(project: Project) -> None:
            self.assertEqual(0, project.run("backup-ords").returncode)
            self.assertFalse([line for line in project.logged() if line.startswith(("tables|", "code|"))])
        self.each_shell(body, ords=ORDS_SAME)

    def test_an_ords_profile_in_the_table_schema_keeps_that_schemas_other_mirrors(self) -> None:
        def body(project: Project) -> None:
            before = tree(project.root)
            result = project.run("backup-ords")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            after = tree(project.root)
            self.assertEqual(self.installed_export("DEMO"), after["database/DEMO/ords/schema.sql"])
            for path in ("database/DEMO/tables/OLD_TABLE.sql", "database/DEMO/views/OLD_VIEW.sql", "database/DEMO/manifest-tables.txt"):
                self.assertEqual(before[path], after[path], path)
        self.each_shell(body, ords=ORDS_SAME)

    def test_without_an_ords_profile_it_refuses_before_touching_anything(self) -> None:
        def body(project: Project) -> None:
            before = tree(project.root)
            result = project.run("backup-ords")
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("ORDS is not configured", plain(result.stderr))
            self.assertEqual([], project.logged(), "SQLcl must not be started")
            self.assertEqual(before, tree(project.root))
        self.each_shell(body, ords="")

    def test_a_partial_profile_is_refused_by_the_loader(self) -> None:
        def body(project: Project) -> None:
            result = project.run("backup-ords")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("must be configured together", plain(result.stderr))
            self.assertEqual([], project.logged())
        self.each_shell(body, ords="\nORDS_SCHEMA=REST_API\n")

    def test_backup_ords_takes_no_arguments(self) -> None:
        def body(project: Project) -> None:
            result = project.run("backup-ords", "extra")
            self.assertEqual(2, result.returncode)
            self.assertIn("backup-ords does not accept arguments", plain(result.stderr))
        self.each_shell(body)

    def test_help_lists_backup_ords(self) -> None:
        def body(project: Project) -> None:
            result = project.run("--help")
            self.assertEqual(0, result.returncode)
            self.assertIn("backup-ords", result.stdout)
            self.assertIn("database/<SCHEMA>/ords/schema.sql", result.stdout)
        self.each_shell(body)

    def test_every_configured_ords_schema_is_exported_by_default_and_installed_together(self) -> None:
        def body(project: Project) -> None:
            result = project.run("backup-ords")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            after = tree(project.root)
            self.assertEqual(self.installed_export("REST_ONE"), after["database/REST_ONE/ords/schema.sql"])
            self.assertEqual(self.installed_export("REST_TWO"), after["database/REST_TWO/ords/schema.sql"])
            self.assertEqual(["version", "ords|REST_ONE|REST_ONE|dev-one|ok", "ords|REST_TWO|REST_TWO|dev-two|ok"], project.logged())
        self.each_shell(body, ords=ORDS_TWO)

    def test_schema_selects_one_ords_only_schema(self) -> None:
        def body(project: Project) -> None:
            before = tree(project.root)
            result = project.run("backup-ords", "--schema", "REST_TWO")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            after = tree(project.root)
            self.assertEqual(self.installed_export("REST_TWO"), after["database/REST_TWO/ords/schema.sql"])
            self.assertEqual(before["database/REST_ONE/ords/schema.sql"], after["database/REST_ONE/ords/schema.sql"])
            self.assertEqual(["version", "ords|REST_TWO|REST_TWO|dev-two|ok"], project.logged())
        self.each_shell(body, ords=ORDS_TWO)

    def test_schema_that_the_ords_profile_does_not_list_is_refused(self) -> None:
        def body(project: Project) -> None:
            result = project.run("backup-ords", "--schema", "DEMO")
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("the ORDS profile does not list schema DEMO", plain(result.stderr))
            self.assertEqual([], project.logged())
        self.each_shell(body)

    def test_a_verified_empty_schema_is_a_valid_result(self) -> None:
        for scenario, expected in (
            ("empty", "verified empty (REST enabled"),
            ("not-enabled-empty", "verified empty (the schema is not REST enabled"),
        ):
            def body(project: Project, expected: str = expected) -> None:
                result = project.run("backup-ords")
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn(expected, plain(result.stdout))
                installed = (project.root / "database/REST_API/ords/schema.sql").read_text(encoding="utf-8")
                self.assertNotEqual("-- old ords for REST_API\n", installed)
                self.assertTrue(installed.endswith("\n"))
            with self.subTest(scenario=scenario):
                self.each_shell(body, config={"ords": {"REST_API": {"scenario": scenario}}})

    def test_every_untrustworthy_export_is_refused_and_the_previous_mirror_kept(self) -> None:
        cases = {
            "wrong-owner-silent": "not the REST schema owner REST_API",
            "wrong-owner-exit1": "failed in SQLcl",
            "exit1": "failed in SQLcl",
            "error-exit0": "SQLcl reported an error",
            "old-ords": "unsupported ORDS release",
            "no-files": "export file is missing or empty",
            "truncated": "truncated",
            "no-inventory": "inventory",
            "drift-counts": "changed while it was exported",
            "drift-content": "two consecutive exports differ",
            "oauth-clients": "failed in SQLcl",
            "no-complete": "did not run to the end",
            "not-enabled-inconsistent": "inventory is inconsistent",
        }
        for scenario, message in cases.items():
            def body(project: Project, message: str = message) -> None:
                before = tree(project.root)
                result = project.run("backup-ords")
                output = plain(result.stdout + result.stderr)
                self.assertEqual(2, result.returncode, output)
                self.assertIn(message, output)
                self.assertIn("the mirror was not replaced", output)
                self.assertEqual(before, tree(project.root), "the previous mirror must be preserved byte for byte")
                self.assertEqual([], project.leftovers())
                self.assertEqual(
                    "", subprocess.run(["git", "-C", str(project.root), "status", "--porcelain", "--", "database"], text=True, capture_output=True).stdout
                )
            with self.subTest(scenario=scenario):
                self.each_shell(body, config={"ords": {"REST_API": {"scenario": scenario}}})

    def test_an_unsupported_sqlcl_release_is_refused_before_any_export_session(self) -> None:
        def body(project: Project) -> None:
            before = tree(project.root)
            result = project.run("backup-ords")
            output = plain(result.stdout + result.stderr)
            self.assertEqual(2, result.returncode, output)
            self.assertIn("SQLcl 25.3.0.0 is not supported for the ORDS export: SQLcl 26.1 or newer is required", output)
            self.assertEqual(["version"], project.logged())
            self.assertEqual(before, tree(project.root))
        self.each_shell(body, config={"version": "SQLcl: Release 25.3.0.0 Production Build: 25.3.0.1"})

    def test_a_failure_on_a_later_schema_installs_nothing(self) -> None:
        def body(project: Project) -> None:
            before = tree(project.root)
            result = project.run("backup-ords")
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("ORDS export verification failed for REST_TWO", plain(result.stderr))
            self.assertEqual(before, tree(project.root), "REST_ONE verified, but nothing may be installed")
        self.each_shell(body, ords=ORDS_TWO, config={"ords": {"REST_TWO": {"scenario": "truncated"}}})

    def test_a_dirty_ords_mirror_is_refused_before_sqlcl_starts(self) -> None:
        def body(project: Project) -> None:
            (project.root / "database/REST_API/ords/schema.sql").write_text("-- hand edit\n", encoding="utf-8")
            for extra in (False, True):
                if extra:
                    subprocess.run(["git", "-C", str(project.root), "checkout", "--", "database"], check=True)
                    (project.root / "database/REST_API/ords/notes.sql").write_text("-- untracked\n", encoding="utf-8")
                result = project.run("backup-ords")
                output = plain(result.stdout + result.stderr)
                self.assertEqual(2, result.returncode, output)
                self.assertIn("dirty mirror: database/REST_API/ords", output)
                self.assertEqual([], project.logged(), "SQLcl must not be started")
            self.assertEqual("-- untracked\n", (project.root / "database/REST_API/ords/notes.sql").read_text(encoding="utf-8"))
        self.each_shell(body)

    def test_a_dirty_table_mirror_does_not_block_backup_ords(self) -> None:
        def body(project: Project) -> None:
            (project.root / "database/REST_API/tables/KEEP_TABLE.sql").write_text("-- being edited\n", encoding="utf-8")
            result = project.run("backup-ords")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual("-- being edited\n", (project.root / "database/REST_API/tables/KEEP_TABLE.sql").read_text(encoding="utf-8"))
        self.each_shell(body)

    def test_a_first_export_creates_the_mirror(self) -> None:
        def body(project: Project) -> None:
            result = project.run("backup-ords")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(self.installed_export(), (project.root / "database/REST_API/ords/schema.sql").read_bytes())
        self.each_shell(body, seed={"database/DEMO/tables/OLD_TABLE.sql": "old\n"})

    @unittest.skipIf(os.name == "nt", "needs POSIX process groups and signals; the Windows Ctrl-C behaviour is covered by test_windows_support")
    def test_interruption_preserves_every_mirror_and_removes_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            started = Path(temporary) / "started"
            project = Project(Path(temporary) / "repo", "bash", config={"ords": {"REST_API": {"scenario": "hang", "started": str(Path(temporary) / "started")}}})
            before = tree(project.root)
            process = subprocess.Popen(
                project.command("backup-ords"), cwd=project.root, env=project.environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
            )
            try:
                deadline = time.monotonic() + 60
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), "SQLcl never started")
                os.killpg(process.pid, signal.SIGINT)
                process.communicate(timeout=60)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
            self.assertEqual(130, process.returncode)
            self.assertEqual(before, tree(project.root))
            self.assertEqual([], project.leftovers())


class BackupDbWithOrdsTests(OrdsCliTestCase):
    def test_backup_db_refreshes_table_code_and_ords_mirrors_together(self) -> None:
        def body(project: Project) -> None:
            result = project.run("backup-db")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            after = tree(project.root)
            self.assertEqual(b"CREATE FAKE FAKE_TABLE FOR DEMO;\n", after["database/DEMO/tables/FAKE_TABLE.sql"])
            self.assertEqual(b"CREATE FAKE FAKE_VIEW FOR DEMO;\n", after["database/DEMO/views/FAKE_VIEW.sql"])
            self.assertNotIn("database/DEMO/tables/OLD_TABLE.sql", after, "the whole-schema mirror is still replaced")
            self.assertEqual(self.installed_export(), after["database/REST_API/ords/schema.sql"])
            self.assertEqual(b"keep this\n", after["database/REST_API/tables/KEEP_TABLE.sql"], "an ORDS-only schema keeps its other mirrors")
            self.assertEqual(b"-- old ords for DEMO\n", after["database/DEMO/ords/schema.sql"], "ORDS not selected for DEMO: its export is carried over")
            self.assertEqual(["version", "tables|DEMO|docker-demo", "code|DEMO|docker-demo", "ords|REST_API|REST_API|dev-rest|ok"], project.logged())
            self.assertEqual([], project.leftovers())
        self.each_shell(body)

    def test_ords_in_the_same_schema_is_installed_inside_the_whole_mirror(self) -> None:
        def body(project: Project) -> None:
            result = project.run("backup-db")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            after = tree(project.root)
            self.assertEqual(self.installed_export("DEMO"), after["database/DEMO/ords/schema.sql"])
            self.assertIn("database/DEMO/tables/FAKE_TABLE.sql", after)
            self.assertNotIn("database/DEMO/tables/OLD_TABLE.sql", after)
        self.each_shell(body, ords=ORDS_SAME)

    def test_without_an_ords_profile_backup_db_is_unchanged_and_keeps_an_existing_ords_folder(self) -> None:
        def body(project: Project) -> None:
            result = project.run("backup-db")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["tables|DEMO|docker-demo", "code|DEMO|docker-demo"], project.logged(), "no SQLcl version probe, no ORDS session")
            after = tree(project.root)
            self.assertIn("database/DEMO/tables/FAKE_TABLE.sql", after)
            self.assertNotIn("database/DEMO/tables/OLD_TABLE.sql", after)
            self.assertEqual(b"-- old ords for DEMO\n", after["database/DEMO/ords/schema.sql"])
        self.each_shell(body, ords="")

    def test_schema_narrows_backup_db_and_preserves_the_ords_mirror_of_an_unselected_scope(self) -> None:
        def body(project: Project) -> None:
            before = tree(project.root)
            result = project.run("backup-db", "--schema", "DEMO")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            after = tree(project.root)
            self.assertEqual(b"CREATE FAKE FAKE_TABLE FOR DEMO;\n", after["database/DEMO/tables/FAKE_TABLE.sql"])
            self.assertEqual(before["database/DEMO/ords/schema.sql"], after["database/DEMO/ords/schema.sql"])
            self.assertEqual(before["database/REST_API/ords/schema.sql"], after["database/REST_API/ords/schema.sql"])
            self.assertEqual(["tables|DEMO|docker-demo", "code|DEMO|docker-demo"], project.logged())
        self.each_shell(body)

    def test_schema_selecting_an_ords_only_schema_exports_only_its_ords(self) -> None:
        def body(project: Project) -> None:
            before = tree(project.root)
            result = project.run("backup-db", "--schema", "REST_API")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            after = tree(project.root)
            self.assertEqual(self.installed_export(), after["database/REST_API/ords/schema.sql"])
            self.assertEqual({"database/REST_API/ords/schema.sql"}, {path for path in after if after[path] != before.get(path)})
        self.each_shell(body)

    def test_an_ords_failure_installs_no_table_or_code_mirror(self) -> None:
        for scenario in ("truncated", "drift-counts", "wrong-owner-silent", "error-exit0"):
            def body(project: Project) -> None:
                before = tree(project.root)
                result = project.run("backup-db")
                self.assertEqual(2, result.returncode, plain(result.stdout + result.stderr))
                self.assertIn("the mirror was not replaced", plain(result.stdout + result.stderr))
                self.assertEqual(before, tree(project.root), "no partial installation")
                self.assertEqual([], project.leftovers())
            with self.subTest(scenario=scenario):
                self.each_shell(body, config={"ords": {"REST_API": {"scenario": scenario}}})

    def test_a_table_or_code_failure_means_no_ords_session_and_no_installation(self) -> None:
        for scope in ("tables", "code"):
            def body(project: Project) -> None:
                before = tree(project.root)
                result = project.run("backup-db")
                self.assertEqual(2, result.returncode, plain(result.stdout + result.stderr))
                self.assertEqual(before, tree(project.root))
                self.assertFalse([line for line in project.logged() if line.startswith("ords|")])
            with self.subTest(scope=scope):
                self.each_shell(body, config={"db": {scope: "exit1"}})

    def test_an_unsupported_sqlcl_release_stops_backup_db_before_any_session(self) -> None:
        def body(project: Project) -> None:
            before = tree(project.root)
            result = project.run("backup-db")
            self.assertEqual(2, result.returncode, plain(result.stdout + result.stderr))
            self.assertIn("SQLcl 26.1 or newer is required", plain(result.stdout + result.stderr))
            self.assertEqual(["version"], project.logged())
            self.assertEqual(before, tree(project.root))
        self.each_shell(body, config={"version": "SQLcl: Release 24.3.1.311.0954 Production"})

    def test_a_dirty_ords_mirror_blocks_a_whole_schema_backup_and_an_ords_only_one(self) -> None:
        def body(project: Project) -> None:
            (project.root / "database/DEMO/ords/schema.sql").write_text("-- hand edit\n", encoding="utf-8")
            (project.root / "database/REST_API/ords/schema.sql").write_text("-- hand edit\n", encoding="utf-8")
            result = project.run("backup-db")
            output = plain(result.stdout + result.stderr)
            self.assertEqual(2, result.returncode, output)
            self.assertIn("dirty mirror", output)
            self.assertEqual([], project.logged())
        self.each_shell(body)


class DoctorOrdsTests(OrdsCliTestCase):
    def test_doctor_checks_the_ords_target_with_the_strict_script(self) -> None:
        def body(project: Project) -> None:
            result = project.run("doctor")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            logged = project.logged()
            self.assertIn("version", logged)
            self.assertIn("doctor|doctor.sql|DEMO|DEMO|docker-demo", logged)
            self.assertIn("doctor|doctor_ords.sql|REST_API|REST_API|dev-rest", logged)
            self.assertIn("Doctor checks passed for all 2 configured DEV schema connections.", plain(result.stdout))
        self.each_shell(body)

    def test_the_ords_check_is_not_replaced_by_another_profiles_check_of_the_same_identity(self) -> None:
        def body(project: Project) -> None:
            result = project.run("doctor")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            logged = project.logged()
            self.assertEqual(1, logged.count("doctor|doctor.sql|DEMO|DEMO|docker-demo"))
            self.assertEqual(1, logged.count("doctor|doctor_ords.sql|DEMO|DEMO|docker-demo"))
        self.each_shell(body, ords=ORDS_SAME)

    def test_a_wrong_ords_owner_fails_doctor(self) -> None:
        def body(project: Project) -> None:
            result = project.run("doctor")
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("1 of 2 doctor check(s) failed", plain(result.stderr))
        self.each_shell(body, config={"ords": {"REST_API": {"scenario": "wrong-owner-exit1"}}})

    def test_old_sqlcl_reports_independent_apex_and_ords_refusals(self) -> None:
        def body(project: Project) -> None:
            result = project.run("doctor")
            output = plain(result.stdout + result.stderr)
            self.assertEqual(2, result.returncode, output)
            self.assertIn("SQLcl 26.1 or newer is required", output)
            self.assertIn("SQLcl 26.3.0.0", output)
            self.assertIn("2 of 2 doctor check(s) failed", output)
            self.assertFalse([line for line in project.logged() if line.startswith("doctor|doctor_ords.sql")])
        self.each_shell(body, config={"version": "SQLcl: Release 25.1.0.0 Production"})

    def test_without_ords_profile_doctor_still_checks_apex_release(self) -> None:
        def body(project: Project) -> None:
            result = project.run("doctor")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["version", "doctor|doctor.sql|DEMO|DEMO|docker-demo"], project.logged())
        self.each_shell(body, ords="")

    def test_schema_narrows_doctor_to_the_ords_only_schema(self) -> None:
        def body(project: Project) -> None:
            result = project.run("doctor", "--schema", "REST_TWO")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["version", "doctor|doctor_ords.sql|REST_TWO|REST_TWO|dev-two"], project.logged())
        self.each_shell(body, ords=ORDS_TWO)

    def test_ords_only_selection_accepts_sqlcl_below_the_apex_floor(self) -> None:
        def body(project: Project) -> None:
            result = project.run("doctor", "--schema", "REST_TWO")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["version", "doctor|doctor_ords.sql|REST_TWO|REST_TWO|dev-two"], project.logged())
        self.each_shell(body, ords=ORDS_TWO, config={"version": "SQLcl: Release 26.2.2.0 Production"})


class SchemaConstantTests(unittest.TestCase):
    def test_the_fixture_schema_is_the_one_the_profiles_name(self) -> None:
        self.assertEqual("REST_API", SCHEMA)


if __name__ == "__main__":
    unittest.main()
