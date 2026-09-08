from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from teamlib.config import Target
from teamlib.masters import MasterError, apex_component_resolver, parse_subscriptions, validate_masters


class MasterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="docker-demo", instance_id="FREE", db_name="FREEPDB1",
            service="freep1", session_user="DEMO", current_schema="DEMO",
            alias="checkout", workspace_id=5402650006222933, app_id=100,
            parsing_schema="DEMO", ownership_mode="shared", binding_digest="a" * 64,
        )
        self.contract = {
            "version": 1,
            "masters": [
                {
                    "app_id": 500,
                    "alias": "master-app",
                    "workspace_id": 5402650006222933,
                    "components": [
                        {"type": "authentication", "symbol": "opendoor-master"},
                        {"type": "theme", "symbol": "universal-theme"},
                    ],
                }
            ],
        }

    def test_parser_ignores_comments_and_quoted_sql(self):
        source = {
            "application.apx": b'''app CHECKOUT {
              value: "subscription { master: @/999/not-real }"
              // subscription { master: @/999/not-real }
            }''',
            "shared-components/auth.apx": b'''shared-component authentication opendoor-master {\n subscription { master: @/500/opendoor-master }\n}''',
        }
        refs = parse_subscriptions(source)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].master_app_id, 500)
        self.assertEqual(refs[0].symbol, "opendoor-master")
        self.assertEqual(refs[0].component_type, "authentication")

    def test_validates_component_type_and_symbol(self):
        report = validate_masters(
            {"shared-components/auth.apx": b"authentication opendoor-master { subscription { master: @/500/opendoor-master } }"},
            self.target,
            self.contract,
        )
        self.assertTrue(report.valid)
        self.assertEqual(report.references[0].master_app_id, 500)

    def test_wrong_component_or_missing_master_refuses(self):
        source = {"x.apx": b"authorization opendoor-master { subscription { master: @/500/opendoor-master } }"}
        with self.assertRaises(MasterError):
            validate_masters(source, self.target, self.contract)
        source = {"x.apx": b"authentication opendoor-master { subscription { master: @/501/opendoor-master } }"}
        with self.assertRaises(MasterError):
            validate_masters(source, self.target, self.contract)

    def test_malformed_subscription_is_rejected(self):
        with self.assertRaises(MasterError):
            parse_subscriptions({"x.apx": b"subscription { master: @/bad/symbol }"})

    def test_builtin_theme_contract_is_explicit(self):
        contract = {
            "version": 1,
            "masters": [{"app_id": 0, "alias": "universal-theme", "workspace_id": None, "builtin": True,
                         "components": [{"type": "theme", "symbol": "universal-theme"}]}],
        }
        report = validate_masters(
            {"x.apx": b"theme universal-theme { subscription { master: @/0/universal-theme } }"},
            self.target,
            contract,
        )
        self.assertTrue(report.valid)

    def test_live_resolver_requires_application_workspace_alias_and_component(self):
        source = {
            "shared-components/auth.apx": b"authentication opendoor-master { subscription { master: @/500/opendoor-master } }"
        }

        def runner(_target, _operation, _driver, _work):
            return SimpleNamespace(
                stdout="TEAM_MASTER_APP|1|1|1\nTEAM_MASTER_COMPONENT|1\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            report = validate_masters(
                source,
                self.target,
                self.contract,
                component_resolver=apex_component_resolver(runner=runner, work_root=Path(directory)),
            )
        self.assertTrue(report.valid)

        def missing_component_runner(_target, _operation, _driver, _work):
            return SimpleNamespace(stdout="TEAM_MASTER_APP|1|1|1\nTEAM_MASTER_COMPONENT|0\n")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MasterError):
                validate_masters(
                    source,
                    self.target,
                    self.contract,
                    component_resolver=apex_component_resolver(
                        runner=missing_component_runner,
                        work_root=Path(directory),
                    ),
                )


if __name__ == "__main__":
    unittest.main()
