import os
import re
import shutil
import subprocess
import _test_subprocess_safety  # noqa: F401
import tempfile
import unittest
from pathlib import Path
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
import fake_sqlcl
from fake_sqlcl import BASH


ROOT = Path(__file__).resolve().parents[1]
PWSH = _test_subprocess_safety.working_pwsh()
# On Windows both wrappers pass '%' for the every-object prefix '*', because the SQLcl launcher there
# expands an unquoted '*'; backup_db.sql treats the two the same.
EVERY_PREFIX = "%" if os.name == "nt" else "*"


def plain(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return re.sub(r"\s*\n\s*\|?\s*", " ", text)


class BackupDbCliTests(unittest.TestCase):
    def make_checkout(self, root: Path, powershell: bool) -> Path:
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        if powershell:
            names = (
                "backup_db.ps1",
                "backup_db.sql",
                "backup_manifest.py",
                "load_env.ps1",
                "check_db_target.ps1",
                "invoke_sqlcl.ps1", "resolve_python.ps1",
                "replace_mirror.ps1",
            )
        else:
            names = (
                "backup_db.sh",
                "backup_db.sql",
                "backup_manifest.py",
                "load_env.sh",
                "check_db_target.sh",
                "sqlcl_safe.sh",
                "replace_mirror.sh",
            )
        for name in names:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        shutil.copy2(ROOT / "template-manifest.json", root / "template-manifest.json")
        if (ROOT / "scripts" / "template_release.ps1").exists():
            shutil.copy2(ROOT / "scripts" / "template_release.ps1", scripts / "template_release.ps1")

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
            "tables_prefixes=\"${12:-}\"\n"
            "if [[ -n \"${FAKE_SQL_ARGS:-}\" ]]; then printf '%s|%s|%s|%s|%s\\n' \"$scope\" \"$schema\" \"${10:-}\" \"$tables_prefixes\" \"$spool_schema\" >> \"$FAKE_SQL_ARGS\"; fi\n"
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
            "# FAKE_SHARED_TRIGGER: both scopes write the same triggers/ file, which the merge must refuse.\n"
            "if [[ -n \"${FAKE_SHARED_TRIGGER:-}\" ]]; then\n"
            "  mkdir -p \"database/$spool_schema/triggers\"\n"
            "  printf 'CREATE SHARED TRIGGER;\\n' > \"database/$spool_schema/triggers/SHARED_TRG.sql\"\n"
            "fi\n"
            "manifest=\"database/$spool_schema/manifest-$scope.txt\"\n"
            "manifest_mode=\"${FAKE_BACKUP_MANIFEST:-valid}\"\n"
            "if [[ \"$scope\" == tables ]]; then\n"
            "  if [[ \"$manifest_mode\" == all-zero ]]; then\n"
            "    printf 'SEQUENCE=0\\nTABLE=0\\nTRIGGER=0\\n' > \"$manifest\"\n"
            "  elif [[ \"$manifest_mode\" == crlf-padded ]]; then\n"
            "    printf ' \\tSEQUENCE=0 \\t\\r\\n  \\tTABLE=1 \\t\\r\\nTRIGGER=0\\r\\n \\t\\r\\n' > \"$manifest\"\n"
            "    printf 'CREATE FAKE OBJECT;\\n' > \"database/$spool_schema/$scope_dir/FAKE_TABLE.sql\"\n"
            "  else\n"
            "    printf 'SEQUENCE=0\\nTABLE=1\\nTRIGGER=0\\n' > \"$manifest\"\n"
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
            '    ascii-padded) printf \'\\v\\f FUNCTION \\f = \\v0 \\f\\v\\r\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=1\\n\\v\\f\\n\' > "$manifest" ;;\n'
            '    unicode-space) printf \'\\302\\240FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=1\\n\' > "$manifest" ;;\n'
            '    unicode-separator) printf \'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=1\\342\\200\\250\' > "$manifest" ;;\n'
            '    bom) printf \'\\357\\273\\277FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=1\\n\' > "$manifest" ;;\n'
            '    bare-cr) printf \'FUNCTION=0\\rPACKAGE=0\\rPACKAGE BODY=0\\rPROCEDURE=0\\rSYNONYM=0\\rTRIGGER=0\\rVIEW=1\\r\' > "$manifest" ;;\n'
            "    *) printf 'FUNCTION=0\\nPACKAGE=0\\nPACKAGE BODY=0\\nPROCEDURE=0\\nSYNONYM=0\\nTRIGGER=0\\nVIEW=1\\n' > \"$manifest\" ;;\n"
            "  esac\n"
            "fi\n"
            "if [[ -n \"${FAKE_MANIFEST_TABLES:-}\" && \"$scope\" == tables ]]; then printf '%s' \"$FAKE_MANIFEST_TABLES\" > \"$manifest\"; fi\n"
            "if [[ -n \"${FAKE_MANIFEST_CODE:-}\" && \"$scope\" == code ]]; then printf '%s' \"$FAKE_MANIFEST_CODE\" > \"$manifest\"; fi\n",
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
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{script.parents[1] / 'bin'}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(script.parents[1] / ".env")
        if manifest is not None:
            environment["FAKE_BACKUP_MANIFEST"] = manifest
        environment.update(extra_env or {})
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
        self.assertEqual((mirror / "manifest-tables.txt").read_text(encoding="utf-8"), "SEQUENCE=0\nTABLE=1\nTRIGGER=0\n")
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
            "bom",
            "bare-cr",
            "unicode-space",
            "unicode-separator",
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
            for manifest in ("all-zero", "crlf-padded", "leading-eight", "ascii-padded"):
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

    # A committed mirror with every output of both scopes plus the ORDS export. triggers/ is written by both
    # scopes: a tables-only run must not carry the committed trigger over, and a both-scopes run replaces it.
    COMMITTED_MIRROR = {
        "tables/OLD_TABLE.sql": b"old table bytes\n",
        "triggers/OLD_TRIGGER.sql": b"CREATE TRIGGER OLD_TRIGGER;\n",
        "views/OLD_VIEW.sql": b"old view bytes\n",
        "packages/OLD_PACKAGE.sql": b"old package bytes\n",
        "ords/OLD_MODULE.json": b'{"module": "old"}\n',
        "manifest-tables.txt": b"OLD TABLE MANIFEST\n",
        "manifest-code.txt": b"OLD CODE MANIFEST\n",
    }
    FAKE_CODE_MANIFEST = "FUNCTION=0\nPACKAGE=0\nPACKAGE BODY=0\nPROCEDURE=0\nSYNONYM=0\nTRIGGER=0\nVIEW=1\n"

    def set_env_values(self, script: Path, values: dict[str, str]) -> None:
        env_path = script.parents[1] / ".env"
        lines = []
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0]
            lines.append(f"{key}={values[key]}" if key in values and "=" in line else line)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def seed_committed_mirror(self, script: Path, files: dict[str, bytes]) -> Path:
        root = script.parents[1]
        mirror = root / "database" / "DEMO$"
        for relative, data in files.items():
            target = mirror / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        subprocess.run(["git", "-C", str(root), "add", "database"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed committed mirror"], check=True)
        return mirror

    def snapshot(self, mirror: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(mirror)).replace(os.sep, "/"): path.read_bytes()
            for path in mirror.rglob("*")
            if path.is_file()
        }

    def test_tables_only_run_keeps_the_committed_code_scope_and_ords_export(self) -> None:
        for wrapper, powershell in self.wrappers():
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as temporary:
                script = self.make_checkout(Path(temporary), powershell=powershell)
                # DEMO$ lists the tables scope only: the code scope belongs to another schema.
                self.set_env_values(script, {"CODE_SCHEMA": "CODE_OWNER", "CODE_EXPECTED_USER": "CODE_OWNER"})
                mirror = self.seed_committed_mirror(script, self.COMMITTED_MIRROR)
                committed = self.snapshot(mirror)

                result = self.run_backup(script, powershell)

                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                current = self.snapshot(mirror)
                # The tables scope ran, so it is replaced whole.
                self.assertEqual(current["tables/FAKE_TABLE.sql"], b"CREATE FAKE OBJECT;\n", output)
                self.assertEqual(current["manifest-tables.txt"], b"SEQUENCE=0\nTABLE=1\nTRIGGER=0\n", output)
                self.assertNotIn("tables/OLD_TABLE.sql", current)
                # The code scope did not run: its outputs and manifest are carried over unchanged.
                for relative in ("views/OLD_VIEW.sql", "packages/OLD_PACKAGE.sql", "manifest-code.txt"):
                    self.assertEqual(current.get(relative), committed[relative], f"{wrapper}: {relative}")
                self.assertEqual(current.get("ords/OLD_MODULE.json"), committed["ords/OLD_MODULE.json"], wrapper)

    def test_code_only_run_keeps_the_committed_tables_scope_and_ords_export(self) -> None:
        for wrapper, powershell in self.wrappers():
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as temporary:
                script = self.make_checkout(Path(temporary), powershell=powershell)
                # DEMO$ lists the code scope only: the tables scope belongs to another schema.
                self.set_env_values(script, {"TABLES_SCHEMA": "TABLE_OWNER", "TABLES_EXPECTED_USER": "TABLE_OWNER"})
                mirror = self.seed_committed_mirror(script, self.COMMITTED_MIRROR)
                committed = self.snapshot(mirror)

                result = self.run_backup(script, powershell)

                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                current = self.snapshot(mirror)
                # The code scope ran, so it is replaced whole: the stale view and package are gone.
                self.assertEqual(current["views/FAKE_VIEW.sql"], b"CREATE FAKE OBJECT;\n", output)
                self.assertEqual(current["manifest-code.txt"], self.FAKE_CODE_MANIFEST.encode(), output)
                self.assertNotIn("views/OLD_VIEW.sql", current)
                self.assertNotIn("packages/OLD_PACKAGE.sql", current)
                # The tables scope did not run: its output and manifest are carried over unchanged.
                for relative in ("tables/OLD_TABLE.sql", "manifest-tables.txt"):
                    self.assertEqual(current.get(relative), committed[relative], f"{wrapper}: {relative}")
                self.assertEqual(current.get("ords/OLD_MODULE.json"), committed["ords/OLD_MODULE.json"], wrapper)

    def test_run_with_both_scopes_replaces_both_and_keeps_ords_export(self) -> None:
        for wrapper, powershell in self.wrappers():
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as temporary:
                script = self.make_checkout(Path(temporary), powershell=powershell)
                mirror = self.seed_committed_mirror(script, self.COMMITTED_MIRROR)
                committed = self.snapshot(mirror)

                result = self.run_backup(script, powershell)

                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                current = self.snapshot(mirror)
                self.assertEqual(current["tables/FAKE_TABLE.sql"], b"CREATE FAKE OBJECT;\n", output)
                self.assertEqual(current["views/FAKE_VIEW.sql"], b"CREATE FAKE OBJECT;\n", output)
                self.assertEqual(current["manifest-tables.txt"], b"SEQUENCE=0\nTABLE=1\nTRIGGER=0\n", output)
                self.assertEqual(current["manifest-code.txt"], self.FAKE_CODE_MANIFEST.encode(), output)
                for stale in ("tables/OLD_TABLE.sql", "views/OLD_VIEW.sql", "packages/OLD_PACKAGE.sql"):
                    self.assertNotIn(stale, current, f"{wrapper}: {stale} should be replaced")
                # No ORDS scope in this run: the committed export is carried over unchanged.
                self.assertEqual(current.get("ords/OLD_MODULE.json"), committed["ords/OLD_MODULE.json"], wrapper)

    def sql_calls(self, log: Path) -> dict[str, list[str]]:
        # One line per SQLcl call: scope|schema|prefixes|tables-prefixes|spool directory.
        return {line.split("|")[0]: line.split("|") for line in log.read_text(encoding="utf-8").splitlines()}

    def test_both_profiles_pass_tables_prefixes_as_seventh_argument(self) -> None:
        for wrapper, powershell in self.wrappers():
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as temporary:
                script = self.make_checkout(Path(temporary), powershell=powershell)
                self.set_env_values(script, {"TABLES_PREFIXES": "ABSH_"})
                log = Path(temporary) / "sql-calls.txt"

                result = self.run_backup(script, powershell, extra_env={"FAKE_SQL_ARGS": str(log)})

                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                calls = self.sql_calls(log)
                # DEMO$ is in both profiles: the tables scope and the code scope both get TABLES_PREFIXES.
                self.assertEqual(calls["tables"][1:4], ["DEMO$", "ABSH_", "ABSH_"], wrapper)
                self.assertEqual(calls["code"][1:4], ["DEMO$", EVERY_PREFIX, "ABSH_"], wrapper)
                # Each scope spools into its own staging directory.
                self.assertTrue(calls["tables"][4].endswith("-tables"), calls["tables"])
                self.assertTrue(calls["code"][4].endswith("-code"), calls["code"])

    def test_code_only_schema_passes_dash_as_tables_prefixes(self) -> None:
        for wrapper, powershell in self.wrappers():
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as temporary:
                script = self.make_checkout(Path(temporary), powershell=powershell)
                # TABLESONLY$ lists the tables scope only: DEMO$ has no tables scope in this run.
                self.set_env_values(script, {
                    "TABLES_SCHEMA": "TABLESONLY$",
                    "TABLES_EXPECTED_USER": "TABLESONLY$",
                    "TABLES_SQLCL_CONNECTION": "docker-tablesonly",
                })
                log = Path(temporary) / "sql-calls.txt"

                result = self.run_backup(script, powershell, extra_env={"FAKE_SQL_ARGS": str(log)})

                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                calls = self.sql_calls(log)
                # DEMO$ keeps every trigger in its code scope ('-' as the seventh argument).
                self.assertEqual(calls["code"][1:4], ["DEMO$", EVERY_PREFIX, "-"], wrapper)
                self.assertEqual(calls["tables"][1], "TABLESONLY$", wrapper)

    def test_trigger_written_by_both_scopes_is_refused_and_mirror_kept(self) -> None:
        for wrapper, powershell in self.wrappers():
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as temporary:
                script = self.make_checkout(Path(temporary), powershell=powershell)
                mirror, original = self.seed_existing_mirror(script)

                result = self.run_backup(
                    script,
                    powershell,
                    extra_env={
                        "FAKE_SHARED_TRIGGER": "1",
                        # Each scope's manifest counts the shared trigger, so only the merge can refuse it.
                        "FAKE_MANIFEST_TABLES": "SEQUENCE=0\nTABLE=1\nTRIGGER=1\n",
                        "FAKE_MANIFEST_CODE": self.FAKE_CODE_MANIFEST.replace("TRIGGER=0", "TRIGGER=1"),
                    },
                )

                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 2, output)
                self.assertIn(
                    "both the tables and code exports wrote triggers/SHARED_TRG.sql for DEMO$; the mirror was not replaced",
                    plain(output),
                )
                self.assertEqual(self.snapshot(mirror), original, f"{wrapper}: the committed mirror was overwritten")
                self.assertFalse((mirror / "triggers").exists(), wrapper)

    def test_tables_only_run_does_not_carry_over_the_committed_triggers_folder(self) -> None:
        for wrapper, powershell in self.wrappers():
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as temporary:
                script = self.make_checkout(Path(temporary), powershell=powershell)
                # The code scope is not in this run, so the tables scope owns triggers/ and the committed one must not return.
                self.set_env_values(script, {
                    "CODE_SCHEMA": "CODEONLY$",
                    "CODE_EXPECTED_USER": "CODEONLY$",
                    "CODE_SQLCL_CONNECTION": "docker-codeonly",
                })
                mirror = self.seed_committed_mirror(script, self.COMMITTED_MIRROR)

                result = self.run_backup(script, powershell, extra_env={"PROJECT_SCHEMA": "DEMO$"})

                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                current = self.snapshot(mirror)
                self.assertIn("tables/FAKE_TABLE.sql", current, output)
                self.assertNotIn("triggers/OLD_TRIGGER.sql", current, wrapper)
                self.assertFalse(any(path.startswith("triggers/") for path in current), wrapper)

    def test_sql_driver_uses_wrapper_supplied_safe_schema_directory(self) -> None:
        script = (ROOT / "scripts" / "backup_db.sql").read_text(encoding="utf-8")
        self.assertIn("DEFINE spool_schema = '&6'", script)
        self.assertIn("SPOOL database/&&spool_schema/", script)
        self.assertNotIn("SPOOL database/&&target_schema/", script)


    def test_sql_driver_mirrors_private_synonyms_and_counts_them(self) -> None:
        script = (ROOT / "scripts" / "backup_db.sql").read_text(encoding="utf-8")
        self.assertIn("SPOOL database/&&spool_schema/synonyms/", script)
        self.assertIn("GET_DDL(''SYNONYM''", script)
        self.assertIn("SELECT 'SYNONYM', 'code', COUNT(*)", script)
        self.assertIn("'VIEW', 'PACKAGE', 'PACKAGE BODY', 'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SYNONYM'", script)
        # Only the schema's own (private) synonyms: filtered by owner, never PUBLIC.
        self.assertIn(
            "AND objects_to_export.owner = UPPER('&&target_schema')\n  AND objects_to_export.object_type = 'SYNONYM'",
            script,
        )

if __name__ == "__main__":
    unittest.main()
