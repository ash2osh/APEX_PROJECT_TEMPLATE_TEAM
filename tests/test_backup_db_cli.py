import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
import fake_sqlcl
from fake_sqlcl import BASH


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")


class BackupDbCliTests(unittest.TestCase):
    def make_checkout(self, root: Path, powershell: bool) -> Path:
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        if powershell:
            names = (
                "backup_db.ps1",
                "backup_db.sql",
                "load_env.ps1",
                "check_db_target.ps1",
                "invoke_sqlcl.ps1", "resolve_python.ps1",
                "replace_mirror.ps1",
            )
        else:
            names = (
                "backup_db.sh",
                "backup_db.sql",
                "load_env.sh",
                "check_db_target.sh",
                "sqlcl_safe.sh",
                "replace_mirror.sh",
            )
        for name in names:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)

        env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
        env_text = env_text.replace("TABLES_SCHEMA=DEMO", "TABLES_SCHEMA=DEMO$")
        env_text = env_text.replace("CODE_SCHEMA=DEMO", "CODE_SCHEMA=DEMO$")
        (root / ".env").write_text(env_text, encoding="utf-8")

        (root / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Backup Test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "backup@example.test"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)

        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_sql = fake_bin / "sql"
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "cat > /dev/null\n"
            "# SQLcl prints this advisory for backup_db.sql's large LONG setting.\n"
            "printf '%s\\n' 'Warning: This LONG setting may cause Java memory problems.' 'It is recommended to reduce the setting and/or increase the memory available to Java.' 'FAKE SQLCL PROGRESS LINE'\n"
            "schema=\"$6\"\n"
            "scope=\"$7\"\n"
            "spool_schema=\"${11:-}\"\n"
            "if [[ -z \"$spool_schema\" || \"$spool_schema\" == *'$'* ]]; then\n"
            "  printf '%s\\n' 'SP2-0332: Cannot create spool file'\n"
            "  exit 0\n"
            "fi\n"
            "case \"$scope\" in\n"
            "  tables) scope_dir=tables; object=FAKE_TABLE ;;\n"
            "  code) scope_dir=views; object=FAKE_VIEW ;;\n"
            "  *) exit 4 ;;\n"
            "esac\n"
            "mkdir -p \"database/$spool_schema/$scope_dir\"\n"
            "manifest=\"database/$spool_schema/manifest-$scope.txt\"\n"
            "manifest_mode=\"${FAKE_BACKUP_MANIFEST:-valid}\"\n"
            "if [[ \"$scope\" == tables ]]; then\n"
            "  if [[ \"$manifest_mode\" == all-zero ]]; then\n"
            "    printf 'TABLE=0\\n' > \"$manifest\"\n"
            "  elif [[ \"$manifest_mode\" == crlf-padded ]]; then\n"
            "    printf '  \\tTABLE=1 \\t\\r\\n\\r\\n' > \"$manifest\"\n"
            "    printf 'CREATE FAKE OBJECT;\\n' > \"database/$spool_schema/$scope_dir/FAKE_TABLE.sql\"\n"
            "  else\n"
            "    printf 'TABLE=1\\n' > \"$manifest\"\n"
            "    printf 'CREATE FAKE OBJECT;\\n' > \"database/$spool_schema/$scope_dir/FAKE_TABLE.sql\"\n"
            "  fi\n"
            "else\n"
            "  code_file_count=\"${FAKE_BACKUP_CODE_FILE_COUNT:-1}\"\n"
            "  [[ \"$manifest_mode\" == all-zero ]] && code_file_count=0\n"
            "  [[ \"$manifest_mode\" == leading-eight ]] && code_file_count=8\n"
            "  for ((index=1; index<=code_file_count; index++)); do\n"
            "    object_name=FAKE_VIEW\n"
            "    [[ \"$index\" -eq 1 ]] || object_name=\"FAKE_VIEW_$index\"\n"
            "    printf 'CREATE FAKE OBJECT;\\n' > \"database/$spool_schema/$scope_dir/$object_name.sql\"\n"
            "  done\n"
            "  case \"$manifest_mode\" in\n"
            "    invalid-trigger) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=invalid\\nVIEW=1\\n' > \"$manifest\" ;;\n"
            "    negative) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=-0\\nVIEW=1\\n' > \"$manifest\" ;;\n"
            "    plus) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=+0\\nVIEW=1\\n' > \"$manifest\" ;;\n"
            "    decimal) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0.0\\nVIEW=1\\n' > \"$manifest\" ;;\n"
            "    duplicate) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nTRIGGER=0\\nVIEW=1\\n' > \"$manifest\" ;;\n"
            "    duplicate-zero) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=1\\n' > \"$manifest\" ;;\n"
            "    missing) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nVIEW=1\\n' > \"$manifest\" ;;\n"
            "    unknown) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nUNKNOWN=0\\nVIEW=1\\n' > \"$manifest\" ;;\n"
            "    multiple-separators) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=1=0\\n' > \"$manifest\" ;;\n"
            "    overflow-80) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=99999999999999999999999999999999999999999999999999999999999999999999999999999999\\n' > \"$manifest\" ;;\n"
            "    overflow-sum) printf 'FUNCTION=1\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=9223372036854775807\\n' > \"$manifest\" ;;\n"
            "    mismatch-count) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=2\\n' > \"$manifest\" ;;\n"
            "    all-zero) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=0\\n' > \"$manifest\" ;;\n"
            "    crlf-padded) printf ' \\tFUNCTION=0 \\t\\r\\nPACKAGE=0\\r\\nPACKAGE BODY=0\\r\\nPROCEDURE=0\\r\\nSYNONYM=0\\r\\nTRIGGER=0\\r\\nVIEW=1 \\t\\r\\n \\t\\r\\n' > \"$manifest\" ;;\n"
            "    leading-eight) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=08\\n' > \"$manifest\" ;;\n"
            "    *) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=1\\n' > \"$manifest\" ;;\n"
            "  esac\n"
            "fi\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        fake_sqlcl.add_launcher(fake_bin)
        return scripts / ("backup_db.ps1" if powershell else "backup_db.sh")

    def run_backup(
        self,
        script: Path,
        powershell: bool,
        *,
        manifest: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{script.parents[1] / 'bin'}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(script.parents[1] / ".env")
        if manifest is not None:
            environment["FAKE_BACKUP_MANIFEST"] = manifest
        command = (
            [PWSH, "-NoProfile", "-File", str(script)]
            if powershell
            else [BASH, str(script)]
        )
        return subprocess.run(
            command,
            cwd=script.parents[1],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def wrappers(self):
        return [("bash", False)] + ([("pwsh", True)] if PWSH else [])

    def seed_existing_mirror(self, script: Path) -> tuple[Path, dict[str, bytes]]:
        root = script.parents[1]
        mirror = root / "database" / "DEMO$"
        (mirror / "tables").mkdir(parents=True)
        (mirror / "views").mkdir()
        (mirror / "manifest-tables.txt").write_bytes(b"OLD TABLE MANIFEST\r\n")
        (mirror / "manifest-code.txt").write_bytes(b"OLD CODE MANIFEST\r\n")
        (mirror / "tables" / "OLD_TABLE.sql").write_bytes(b"old table bytes\n")
        (mirror / "views" / "OLD_VIEW.sql").write_bytes(b"old code bytes\n")
        subprocess.run(["git", "-C", str(root), "add", "database"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed old mirror"], check=True)
        snapshot = {
            str(path.relative_to(mirror)): path.read_bytes()
            for path in mirror.rglob("*")
            if path.is_file()
        }
        return mirror, snapshot

    def assert_dollar_schema_mirror_installed(self, script: Path, output: str) -> None:
        mirror = script.parents[1] / "database" / "DEMO$"
        self.assertTrue(mirror.is_dir(), output)
        self.assertEqual((mirror / "manifest-tables.txt").read_text(encoding="utf-8"), "TABLE=1\n")
        self.assertEqual(
            (mirror / "manifest-code.txt").read_text(encoding="utf-8"),
            "FUNCTION=0\nPACKAGE=0\nPACKAGE BODY=0\nPROCEDURE=0\nSYNONYM=0\nTRIGGER=0\nVIEW=1\n",
        )
        self.assertTrue((mirror / "tables" / "FAKE_TABLE.sql").is_file())
        self.assertTrue((mirror / "views" / "FAKE_VIEW.sql").is_file())
        # The LONG advisory is dropped; the rest of SQLcl's output is kept.
        self.assertNotIn("LONG setting may cause Java memory problems", output)
        self.assertNotIn("increase the memory available to Java", output)
        self.assertIn("FAKE SQLCL PROGRESS LINE", output)
        database_entries = [path.name for path in (script.parents[1] / "database").iterdir()]
        self.assertEqual(database_entries, ["DEMO$"])

    def test_bash_backup_spools_safely_then_installs_original_dollar_schema_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = self.make_checkout(Path(temporary), powershell=False)

            result = self.run_backup(script, powershell=False)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_dollar_schema_mirror_installed(script, result.stdout + result.stderr)

    @unittest.skipUnless(PWSH, "PowerShell Core is not installed")
    def test_powershell_backup_spools_safely_then_installs_original_dollar_schema_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = self.make_checkout(Path(temporary), powershell=True)

            result = self.run_backup(script, powershell=True)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_dollar_schema_mirror_installed(script, result.stdout + result.stderr)

    def test_invalid_manifest_preserves_existing_mirror_in_both_wrappers(self) -> None:
        invalid_manifests = (
            "invalid-trigger",
            "negative",
            "plus",
            "decimal",
            "duplicate",
            "duplicate-zero",
            "missing",
            "unknown",
            "multiple-separators",
            "overflow-80",
            "overflow-sum",
            "mismatch-count",
        )
        for wrapper, powershell in self.wrappers():
            for manifest in invalid_manifests:
                with self.subTest(wrapper=wrapper, manifest=manifest), tempfile.TemporaryDirectory() as temporary:
                    script = self.make_checkout(Path(temporary), powershell=powershell)
                    mirror, original = self.seed_existing_mirror(script)

                    result = self.run_backup(script, powershell, manifest=manifest)

                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    if manifest == "overflow-sum":
                        self.assertIn("overflows", (result.stdout + result.stderr).lower())
                    current = {
                        str(path.relative_to(mirror)): path.read_bytes()
                        for path in mirror.rglob("*")
                        if path.is_file()
                    }
                    self.assertEqual(current, original)

    def test_valid_manifest_variants_have_bash_powershell_parity(self) -> None:
        for wrapper, powershell in self.wrappers():
            for manifest in ("all-zero", "crlf-padded", "leading-eight"):
                with self.subTest(wrapper=wrapper, manifest=manifest), tempfile.TemporaryDirectory() as temporary:
                    script = self.make_checkout(Path(temporary), powershell=powershell)

                    result = self.run_backup(script, powershell, manifest=manifest)

                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    mirror = script.parents[1] / "database" / "DEMO$"
                    if manifest == "all-zero":
                        self.assertEqual(list((mirror / "tables").glob("*.sql")), [])
                        self.assertEqual(list((mirror / "views").glob("*.sql")), [])
                        self.assertIn("PACKAGE BODY=0", (mirror / "manifest-code.txt").read_text())
                    elif manifest == "leading-eight":
                        self.assertEqual(len(list((mirror / "views").glob("*.sql"))), 8)
                        self.assertIn("VIEW=08", (mirror / "manifest-code.txt").read_text())
                    else:
                        tables_manifest = (mirror / "manifest-tables.txt").read_text()
                        self.assertIn("TABLE=1", tables_manifest)
                        self.assertEqual(len(list((mirror / "tables").glob("*.sql"))), 1)
                        self.assertEqual(len(list((mirror / "views").glob("*.sql"))), 1)
                        self.assertIn("PACKAGE BODY=0", (mirror / "manifest-code.txt").read_text())

    def test_sql_driver_uses_wrapper_supplied_safe_schema_directory(self) -> None:
        script = (ROOT / "scripts" / "backup_db.sql").read_text(encoding="utf-8")
        self.assertIn("DEFINE spool_schema = '&6'", script)
        self.assertIn("SPOOL database/&&spool_schema/", script)
        self.assertNotIn("SPOOL database/&&target_schema/", script)


    def test_sql_driver_mirrors_private_synonyms_and_counts_them(self) -> None:
        script = (ROOT / "scripts" / "backup_db.sql").read_text(encoding="utf-8")
        self.assertIn("SPOOL database/&&spool_schema/synonyms/", script)
        self.assertIn("GET_DDL(''SYNONYM''", script)
        self.assertIn("SELECT 'SYNONYM', 'code' FROM dual", script)
        self.assertIn("'VIEW', 'PACKAGE', 'PACKAGE BODY', 'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SYNONYM'", script)
        # Only the schema's own (private) synonyms: filtered by owner, never PUBLIC.
        self.assertIn(
            "AND objects_to_export.owner = UPPER('&&target_schema')\n  AND objects_to_export.object_type = 'SYNONYM'",
            script,
        )

if __name__ == "__main__":
    unittest.main()
