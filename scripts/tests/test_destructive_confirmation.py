from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from teamlib.destructive_confirmation import (
    ConfirmationError,
    ConfirmationRequirement,
    confirmation_template,
    load_confirmation,
    require_confirmations,
)


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.requirements = (
            ConfirmationRequirement("20260910T120002__alice__z", "undo", "b" * 64, "d" * 64),
            ConfirmationRequirement("20260910T120001__alice__a", "migrate", "a" * 64, "c" * 64),
        )

    def test_exact_document_returns_canonical_digest_and_template_is_sorted(self):
        template = confirmation_template(self.requirements)
        self.assertEqual(
            [entry["migration_id"] for entry in template["confirmations"]],
            ["20260910T120001__alice__a", "20260910T120002__alice__z"],
        )
        document = {
            "version": 1,
            "confirmations": [
                {**entry, "confirmed": True} for entry in template["confirmations"]
            ],
        }
        expected = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        ).hexdigest()
        self.assertEqual(require_confirmations(self.requirements, document), expected)

    def test_closed_schema_and_exact_match_refusals(self):
        valid = {**confirmation_template((self.requirements[0],))["confirmations"][0], "confirmed": True}
        invalid = (
            True,
            {"version": 2, "confirmations": []},
            {"version": 1, "confirmations": [], "extra": 1},
            {"version": 1, "confirmations": [valid, valid]},
            {"version": 1, "confirmations": [dict(valid, confirmed=False)]},
            {"version": 1, "confirmations": [dict(valid, action="redo")]},
            {"version": 1, "confirmations": [dict(valid, bundle_checksum="x")]},
            {"version": 1, "confirmations": [dict(valid, payload_target_state_key="x")]},
            {"version": 1, "confirmations": [dict(valid, confirmed=1)]},
            {"version": 1, "confirmations": [dict(valid, extra=1)]},
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ConfirmationError):
                    require_confirmations(self.requirements[:1], value)

    def test_missing_and_extra_entries_are_reported(self):
        template = confirmation_template(self.requirements)
        one = {**template["confirmations"][0], "confirmed": True}
        with self.assertRaisesRegex(ConfirmationError, "missing"):
            require_confirmations(self.requirements, {"version": 1, "confirmations": [one]})
        extra = {**template["confirmations"][0], "migration_id": "20260910T120003__alice__x", "confirmed": True}
        with self.assertRaisesRegex(ConfirmationError, "extra"):
            require_confirmations(self.requirements, {"version": 1, "confirmations": [
                {**template["confirmations"][0], "confirmed": True}, extra,
            ]})

    def test_no_requirements_need_no_document_and_reject_entries(self):
        self.assertEqual(require_confirmations((), None), "")
        self.assertEqual(require_confirmations((), {"version": 1, "confirmations": []}), "")
        with self.assertRaises(ConfirmationError):
            require_confirmations((), {"version": 1, "confirmations": [
                {**confirmation_template((self.requirements[0],))["confirmations"][0], "confirmed": True}
            ]})

    def test_loader_requires_canonical_regular_file(self):
        document = confirmation_template(self.requirements)
        with tempfile.TemporaryDirectory(prefix="team-confirmation-") as directory:
            root = Path(directory)
            path = root / "confirmation.json"
            path.write_bytes(json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            loaded, digest = load_confirmation(path)
            self.assertEqual(loaded, document)
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            path.write_text(json.dumps(document, indent=2), encoding="utf-8")
            with self.assertRaises(ConfirmationError):
                load_confirmation(path)
            path.write_bytes(b"\xff")
            with self.assertRaises(ConfirmationError):
                load_confirmation(path)


if __name__ == "__main__":
    unittest.main()
