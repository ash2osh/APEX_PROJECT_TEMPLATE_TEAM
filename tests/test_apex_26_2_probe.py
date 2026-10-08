import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.probe_apex_26_2 import Probe, ProbeError, ProbeTarget, require_version_changed_files


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = ProbeTarget("docker-demo", "DEMO", "DEMO", "DEMO", 940262, "local-host", "freepdb1")
        self.calls = []
        self.identity = {"sessionUser": "DEMO", "currentSchema": "DEMO", "serverHost": "local-host",
                         "service": "freepdb1", "workspace": "DEMO", "workspaceId": 123,
                         "apexVersion": "26.2.0", "databaseVersion": "23.26.3.0.0", "appCount": 0}

    def runner(self, target, driver, run_dir):
        self.calls.append(driver.read_text())
        text = 'APEX_PROBE_IDENTITY:' + json.dumps(self.identity) + '\nAPEX_PROBE_VERIFIED:identity\n'
        return SimpleNamespace(output=text)

    def probe(self):
        return Probe(self.target, Path(self.temporary.name), runner=self.runner,
                     version_reader=lambda path: "26.3.0.0")

    def test_probe_defaults_to_read_only(self):
        report = self.probe().run()
        self.assertEqual(report["checks"]["identity"]["status"], "pass")
        self.assertFalse(report["allowWrites"])
        self.assertEqual(len(self.calls), 1)
        script = self.calls[0].lower()
        for command in ("apex import", "lock_application(", "unlock_application(", "create_working_copy", "remove_application(", "commit;"):
            self.assertNotIn(command, script)

    def test_fixture_alias_is_an_uppercase_apexlang_external_identifier(self):
        self.assertRegex(self.probe().alias, r"^[A-Z][A-Z0-9-]+$")

    def test_version_effects_allow_omitted_cutoff_but_refuse_unrelated_source(self):
        require_version_changed_files({"application.apx"})
        require_version_changed_files({"application.apx", "deployments/default.json"})
        for changed in (set(), {"deployments/default.json"}, {"application.apx", "pages/p00001-home.apx"}):
            with self.subTest(changed=changed), self.assertRaises(ProbeError):
                require_version_changed_files(changed)

    def test_wrong_identity_stops_before_write(self):
        self.identity["serverHost"] = "another-host"
        with self.assertRaises(ProbeError):
            self.probe().run(allow_writes=True, source_26_1=Path(self.temporary.name), developers=("DEMO", "OTHER"))
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("apex import", self.calls[0].lower())

    def test_existing_app_id_is_not_overwritten(self):
        self.identity["appCount"] = 1
        with self.assertRaises(ProbeError):
            self.probe().run(allow_writes=True, source_26_1=Path(self.temporary.name), developers=("DEMO", "OTHER"))
        self.assertEqual(len(self.calls), 1)

    def test_unknown_command_cannot_count_as_success(self):
        probe = self.probe()
        probe.runner = lambda *args: SimpleNamespace(output="Unknown Command: apex import\nAPEX_PROBE_VERIFIED:import\n")
        with self.assertRaises(ProbeError):
            probe.execute("import", "apex import", success_line="Import successful.")

    def test_skipped_import_with_only_visibility_marker_is_refused(self):
        probe = self.probe()
        probe.runner = lambda *args: SimpleNamespace(output="APEX_PROBE_VERIFIED:import\n")
        with self.assertRaises(ProbeError):
            probe.execute("import", "apex import", success_line="Import successful.")

    def test_reports_do_not_embed_full_sqlcl_logs(self):
        report = self.probe().run()
        serialized = json.dumps(report)
        self.assertNotIn("output", report)
        self.assertNotIn("APEX_PROBE_IDENTITY", serialized)

    def test_cleanup_refuses_report_for_another_target_before_connecting(self):
        saved = {"schemaVersion": 1, "appId": 999, "runId": "a" * 32,
                 "fixtureAlias": "APEX262-PROBE-999-" + "a" * 12,
                 "identity": self.identity, "developers": ["DEMO", "OTHER"]}
        path = Path(self.temporary.name) / "old-report.json"
        path.write_text(json.dumps(saved))
        with self.assertRaises(ProbeError):
            self.probe().cleanup(path)
        self.assertEqual(self.calls, [])
