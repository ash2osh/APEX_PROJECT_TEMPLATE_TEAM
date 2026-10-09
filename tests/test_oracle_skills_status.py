"""Tests for scripts/oracle_skills_status.py: shared read-only native Oracle skills inspection.

Validates SQLcl native skills registry (~/.dbtools/skills/skills.json) and synced catalog.
Asserts freshness derives from every loaded installation date, never file mtimes.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.oracle_skills_status import inspect_oracle_skills


class OracleSkillsStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: Path(self.temp_dir).exists() and [
            (p.unlink() if p.is_file() else [f.unlink() for f in p.rglob("*") if f.is_file()])
            for p in Path(self.temp_dir).iterdir()
        ] and self._cleanup_dir(Path(self.temp_dir)))
        self.repo_dir = Path(self.temp_dir) / "oracle-skills"
        self.repo_dir.mkdir(parents=True)
        # Create default catalog skills
        for skill in ("apex", "db", "fusion", "graal", "oci"):
            skill_dir = self.repo_dir / skill
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(f"# {skill} skill\n", encoding="utf-8")

        self.loaded_root = Path(self.temp_dir) / "loaded_skills"
        self.loaded_root.mkdir()
        for skill in ("apex", "db", "fusion", "graal", "oci"):
            target_dir = self.loaded_root / skill
            target_dir.mkdir()
            (target_dir / "SKILL.md").write_text(f"# Installed {skill}\n", encoding="utf-8")

        self.fixed_now = datetime(2026, 10, 9, 10, 0, 0, tzinfo=timezone.utc)
        self.valid_date = "2026-10-08T05:13:11.538803622Z"

    def _cleanup_dir(self, directory: Path) -> None:
        import shutil
        shutil.rmtree(directory, ignore_errors=True)

    def write_registry(self, repo_sync: str, installations: list[dict]) -> Path:
        registry_path = Path(self.temp_dir) / "skills.json"
        data = {
            "repositories": [
                {
                    "id": "oracle-skills",
                    "url": "https://github.com/oracle/skills",
                    "localPath": str(self.repo_dir),
                    "lastSync": repo_sync,
                }
            ],
            "installations": installations,
        }
        registry_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return registry_path

    def make_default_installations(self, installed_at: str) -> list[dict]:
        return [
            {
                "skillName": skill,
                "agent": "test-agent",
                "scope": "global",
                "targetPath": str(self.loaded_root / skill),
                "source": "git",
                "sourcePath": str(self.repo_dir / skill),
                "installedAt": installed_at,
            }
            for skill in ("apex", "db", "fusion", "graal", "oci")
        ]

    def test_repositories_null_or_nonlist_reports_invalid(self) -> None:
        registry_path = Path(self.temp_dir) / "skills_null_repos.json"
        registry_path.write_text(json.dumps({
            "repositories": None,
            "installations": self.make_default_installations(self.valid_date),
        }), encoding="utf-8")
        result = inspect_oracle_skills(registry_path, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("repositories" in issue for issue in result["issues"]))

    def test_catalog_path_nonstring_reports_invalid(self) -> None:
        registry_path = Path(self.temp_dir) / "skills_bad_local_path.json"
        registry_path.write_text(json.dumps({
            "repositories": [
                {
                    "id": "oracle-skills",
                    "localPath": 42,
                    "lastSync": self.valid_date,
                }
            ],
            "installations": self.make_default_installations(self.valid_date),
        }), encoding="utf-8")
        result = inspect_oracle_skills(registry_path, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("localPath" in issue for issue in result["issues"]))

    def test_installation_target_path_nonstring_reports_invalid(self) -> None:
        installs = self.make_default_installations(self.valid_date)
        installs[0]["targetPath"] = {"not": "a string"}
        registry_path = Path(self.temp_dir) / "skills_bad_target_path.json"
        registry_path.write_text(json.dumps({
            "repositories": [
                {
                    "id": "oracle-skills",
                    "localPath": str(self.repo_dir),
                    "lastSync": self.valid_date,
                }
            ],
            "installations": installs,
        }), encoding="utf-8")
        result = inspect_oracle_skills(registry_path, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("targetPath" in issue for issue in result["issues"]))

    def test_catalog_root_missing_skill_md_reports_invalid(self) -> None:
        (self.repo_dir / "apex" / "SKILL.md").unlink()
        registry = self.write_registry(self.valid_date, self.make_default_installations(self.valid_date))
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("SKILL.md" in issue and "catalog" in issue for issue in result["issues"]))

    def test_duplicate_different_scope_rejected_as_conflicting_invalid(self) -> None:
        installs = self.make_default_installations(self.valid_date)
        installs.append({
            "skillName": "apex",
            "agent": "another-agent-label",
            "scope": "project",
            "targetPath": str(self.loaded_root / "apex"),
            "source": "git",
            "sourcePath": str(self.repo_dir / "apex"),
            "installedAt": self.valid_date,
        })
        registry = self.write_registry(self.valid_date, installs)
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("conflicting duplicate" in issue for issue in result["issues"]))

    def test_installed_skill_name_mismatch_with_source_path_reports_invalid(self) -> None:
        installs = self.make_default_installations(self.valid_date)
        installs[0]["skillName"] = "wrong_name"
        registry = self.write_registry(self.valid_date, installs)
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("skillName" in issue or "correspond" in issue for issue in result["issues"]))


    def test_valid_skills_registry_is_current(self) -> None:
        registry = self.write_registry(self.valid_date, self.make_default_installations(self.valid_date))
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["installationCount"], 5)
        self.assertEqual(result["oldestInstalledAt"], self.valid_date)
        self.assertEqual(result["issues"], [])

    def test_other_repositories_do_not_invalidate_loaded_oracle_skills(self) -> None:
        installs = self.make_default_installations(self.valid_date)
        installs.append({"skillName": "custom", "agent": "test-agent", "scope": "global",
                         "targetPath": str(self.loaded_root / "custom"), "source": "git",
                         "sourcePath": str(Path(self.temp_dir) / "other-repo/custom"),
                         "installedAt": "2020-01-01T00:00:00Z"})
        registry = self.write_registry(self.valid_date, installs)
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["installationCount"], 5)

    def test_target_skill_name_must_match_even_without_source_path(self) -> None:
        installs = self.make_default_installations(self.valid_date)
        installs[0]["skillName"] = "wrong_name"
        installs[0].pop("sourcePath")
        registry = self.write_registry(self.valid_date, installs)
        self.assertEqual(inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)["status"], "invalid")

    def test_conflicting_oracle_repository_records_are_invalid(self) -> None:
        registry = self.write_registry(self.valid_date, self.make_default_installations(self.valid_date))
        data = json.loads(registry.read_text())
        data["repositories"].append({**data["repositories"][0], "lastSync": "2030-01-01T00:00:00Z"})
        registry.write_text(json.dumps(data))
        self.assertEqual(inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)["status"], "invalid")

    def test_unreadable_catalog_is_a_structured_invalid_result(self) -> None:
        registry = self.write_registry(self.valid_date, self.make_default_installations(self.valid_date))
        with patch.object(Path, "iterdir", side_effect=PermissionError("catalog inaccessible")):
            result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")

    def test_installed_skill_must_be_readable_utf8(self) -> None:
        registry = self.write_registry(self.valid_date, self.make_default_installations(self.valid_date))
        (self.loaded_root / "apex/SKILL.md").write_bytes(b"\xff")
        self.assertEqual(inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)["status"], "invalid")

    def test_exact_seven_day_old_date_is_stale(self) -> None:
        seven_days_ago = (self.fixed_now - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        registry = self.write_registry(self.valid_date, self.make_default_installations(seven_days_ago))
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "stale")
        self.assertTrue(any("refresh is due" in issue or "stale" in issue for issue in result["issues"]))

    def test_more_than_seven_days_old_is_stale(self) -> None:
        eight_days_ago = (self.fixed_now - timedelta(days=8)).isoformat().replace("+00:00", "Z")
        registry = self.write_registry(eight_days_ago, self.make_default_installations(eight_days_ago))
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "stale")

    def test_future_date_rejected_as_invalid(self) -> None:
        future_date = (self.fixed_now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        registry = self.write_registry(self.valid_date, self.make_default_installations(future_date))
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("future" in issue for issue in result["issues"]))

    def test_naive_timestamp_rejected_as_invalid(self) -> None:
        naive_date = "2026-10-08T05:13:11"
        registry = self.write_registry(self.valid_date, self.make_default_installations(naive_date))
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("UTC" in issue or "invalid" in issue or "naive" in issue for issue in result["issues"]))

    def test_invalid_timestamp_string_rejected(self) -> None:
        bad_date = "NOT_A_TIMESTAMP"
        registry = self.write_registry(bad_date, self.make_default_installations(self.valid_date))
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")

    def test_missing_registry_file_reported_as_missing(self) -> None:
        nonexistent = Path(self.temp_dir) / "no-such-skills.json"
        result = inspect_oracle_skills(nonexistent, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["installationCount"], 0)

    def test_malformed_registry_json_reported_as_invalid(self) -> None:
        bad_file = Path(self.temp_dir) / "skills.json"
        bad_file.write_text("{ NOT JSON }", encoding="utf-8")
        result = inspect_oracle_skills(bad_file, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")

    def test_missing_catalog_directory_reported_as_invalid(self) -> None:
        bad_repo_path = Path(self.temp_dir) / "nonexistent-repo"
        registry_path = Path(self.temp_dir) / "skills.json"
        registry_path.write_text(json.dumps({
            "repositories": [{"id": "oracle-skills", "localPath": str(bad_repo_path), "lastSync": self.valid_date}],
            "installations": self.make_default_installations(self.valid_date)
        }))
        result = inspect_oracle_skills(registry_path, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("catalog" in issue for issue in result["issues"]))

    def test_missing_installed_skill_md_reported_as_invalid(self) -> None:
        # Delete SKILL.md for apex
        (self.loaded_root / "apex" / "SKILL.md").unlink()
        registry = self.write_registry(self.valid_date, self.make_default_installations(self.valid_date))
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("SKILL.md" in issue for issue in result["issues"]))

    def test_identical_duplicate_records_deduplicated(self) -> None:
        installs = self.make_default_installations(self.valid_date)
        # Duplicate the apex installation with identical metadata
        installs.append({
            "skillName": "apex",
            "agent": "another-agent-label",
            "scope": "global",
            "targetPath": str(self.loaded_root / "apex"),
            "source": "git",
            "sourcePath": str(self.repo_dir / "apex"),
            "installedAt": self.valid_date,
        })

        registry = self.write_registry(self.valid_date, installs)
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["installationCount"], 5)

    def test_conflicting_duplicate_records_rejected_as_invalid(self) -> None:
        installs = self.make_default_installations(self.valid_date)
        # Duplicate apex with a different installedAt
        installs.append({
            "skillName": "apex",
            "agent": "another-agent-label",
            "scope": "global",
            "targetPath": str(self.loaded_root / "apex"),
            "installedAt": "2026-10-07T05:13:11.538803622Z",
        })
        registry = self.write_registry(self.valid_date, installs)
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("conflicting duplicate" in issue for issue in result["issues"]))

    def test_extra_oracle_catalog_root_requires_installation(self) -> None:
        # Add an extra skill in catalog that isn't installed
        extra_dir = self.repo_dir / "workflow"
        extra_dir.mkdir()
        (extra_dir / "SKILL.md").write_text("# Workflow skill\n", encoding="utf-8")

        registry = self.write_registry(self.valid_date, self.make_default_installations(self.valid_date))
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("workflow" in issue for issue in result["issues"]))

    def test_freshness_derives_from_registry_never_mtime(self) -> None:
        # SKILL.md file has fresh mtime (just written), but registry date is 10 days old
        ten_days_ago = (self.fixed_now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
        registry = self.write_registry(ten_days_ago, self.make_default_installations(ten_days_ago))
        result = inspect_oracle_skills(registry, [self.loaded_root], self.fixed_now)
        self.assertEqual(result["status"], "stale")

        # SKILL.md file has old mtime (e.g. 50 days ago), but registry date is fresh (< 7 days)
        old_time = (self.fixed_now - timedelta(days=50)).timestamp()
        for skill in ("apex", "db", "fusion", "graal", "oci"):
            os.utime(self.loaded_root / skill / "SKILL.md", (old_time, old_time))
        registry_fresh = self.write_registry(self.valid_date, self.make_default_installations(self.valid_date))
        result_fresh = inspect_oracle_skills(registry_fresh, [self.loaded_root], self.fixed_now)
        self.assertEqual(result_fresh["status"], "current")


if __name__ == "__main__":
    unittest.main()
