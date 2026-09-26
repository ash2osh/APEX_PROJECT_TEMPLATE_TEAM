import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "apex-background"
RENDERER = SKILL / "probe" / "render_findings.py"


class RenderFindingsTests(unittest.TestCase):
    def render(self, log: str, faults: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "log.csv"
            faults_path = Path(temporary) / "faults.csv"
            log_path.write_text(log, encoding="utf-8")
            faults_path.write_text(faults, encoding="utf-8")
            return subprocess.run(
                [
                    "python3",
                    str(RENDERER),
                    str(log_path),
                    str(faults_path),
                    "--apex",
                    "26.1.4",
                    "--database",
                    "FREEPDB1",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_matrix_has_one_column_per_context_in_first_seen_order(self) -> None:
        result = self.render(
            '"CONTEXT_NAME","PROBE_NAME","PROBE_VALUE","PROBE_ERROR"\n'
            '"SQLCL_APEX_SESSION","V(APP_SESSION)","1234",\n'
            '"WORKFLOW_START","V(APP_SESSION)","<null>",\n'
            '"SQLCL_APEX_SESSION","BIND :APEX$TASK_ID","",\n'
            '"WORKFLOW_START","DO_SUBSTITUTIONS(&APP_NAME.)",,"ORA-06550: boom"\n',
            '"SOURCE","NAME","DETAIL"\n',
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("APEX 26.1.4", result.stdout)
        self.assertIn("| Probe | SQLCL_APEX_SESSION | WORKFLOW_START |", lines)
        self.assertIn("| V(APP_SESSION) | 1234 | <null> |", lines)
        self.assertIn("| BIND :APEX$TASK_ID | <null> | - |", lines)
        self.assertIn("| DO_SUBSTITUTIONS(&APP_NAME.) | - | error: ORA-06550: boom |", lines)
        self.assertIn("No faults recorded.", result.stdout)

    def test_pipes_in_values_are_escaped_and_faults_listed(self) -> None:
        result = self.render(
            '"CONTEXT_NAME","PROBE_NAME","PROBE_VALUE","PROBE_ERROR"\n'
            '"A","P","x|y",\n',
            '"SOURCE","NAME","DETAIL"\n"workflow activity","binds-start","faulted: ORA-01008"\n',
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| P | x\\|y |", result.stdout)
        self.assertIn("- workflow activity `binds-start`: faulted: ORA-01008", result.stdout)

    def test_missing_header_is_rejected(self) -> None:
        result = self.render('"A","B"\n', '"SOURCE","NAME","DETAIL"\n')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected CSV header", result.stderr)


class ProbeLayoutTests(unittest.TestCase):
    def test_install_defines_every_probe_the_skill_relies_on(self) -> None:
        install = (SKILL / "probe" / "install.sql").read_text(encoding="utf-8")
        for probe in (
            "V(APP_SESSION)",
            "V(APP_USER)",
            "V(PROBE_APP_ITEM)",
            "V(P1_PROBE_ITEM)",
            "APEX_APPLICATION.G_INSTANCE",
            "SYS_CONTEXT(APEX$SESSION,APP_SESSION)",
            "DO_SUBSTITUTIONS(&APP_NAME.)",
            "DO_SUBSTITUTIONS(&PROBE_SUBST.)",
            "USERENV BG_JOB_ID",
            "USERENV MODULE",
        ):
            with self.subTest(probe=probe):
                self.assertIn(f"'{probe}'", install)
        self.assertIn("PRAGMA AUTONOMOUS_TRANSACTION", install)
        self.assertIn("SET DEFINE OFF", install)

    def test_uninstall_removes_every_installed_object(self) -> None:
        uninstall = (SKILL / "probe" / "uninstall.sql").read_text(encoding="utf-8")
        for statement in (
            "DROP PACKAGE apex_bg_probe",
            "DROP TABLE apex_bg_probe_log",
            "DROP TABLE apex_bg_probe_run",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, uninstall)

    def test_uninstall_checks_object_ownership_before_dropping(self) -> None:
        uninstall = (SKILL / "probe" / "uninstall.sql").read_text(encoding="utf-8")
        self.assertIn("APEX_BG_PROBE_OWNER_V1", uninstall)
        self.assertIn("USER_TAB_COMMENTS", uninstall)
        self.assertIn("USER_SOURCE", uninstall)


if __name__ == "__main__":
    unittest.main()
