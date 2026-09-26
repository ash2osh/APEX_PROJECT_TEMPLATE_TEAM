# Template Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Let a project created from this template pull newer template scripts, tests, and agent instructions with `scripts/team.sh upgrade-template`, without overwriting the project's own instruction files, application source, database mirrors, or migrations.

**Architecture:** The template declares file ownership in a tracked `template-manifest.json`: template-owned globs (upgraded), project-owned placeholder files (created once, never overwritten), and template-only files (never copied). Project instructions move into empty placeholder files that the template-owned `AGENTS.md` and a new `CLAUDE.md` import. A stdlib-only Python engine clones the template at a ref, compares three hashes per file (new template, last installed template from a tracked `.template-lock.json`, local file), and updates unmodified files, keeps local customizations, and writes `<file>.template-new` for true conflicts. Bash and PowerShell wrappers expose it through the existing team CLI.

**Tech Stack:** Python 3.10+ standard library, Git CLI, Bash, PowerShell 5.1+, `unittest`.

**Spec:** This plan is the spec. User request (2026-09-26): "Template upgrade: how developers who copied this template update it to reflect the latest version of scripts and instructions, which do not overwrite their own instruction files, which should have empty placeholder files in this template."

## Global Constraints

- Runtime workflow stays stdlib-only Python; no new runtime dependency (`pyproject.toml` `dependencies = []`).
- Every Bash command has a PowerShell 5.1-compatible equivalent (`#Requires -Version 5.1`; no .NET Core-only APIs such as `Path.GetRelativePath`).
- APEXlang, SQL, JSON, `.sh`, `.ps1` files stay LF (`.gitattributes`).
- The upgrade never reads, writes, or deletes `apps/<schema>/<id>/`, `database/`, `migrations/<developer>/`, `app_context/<id>/`, `.env`, or any path not named by the template manifest.
- The upgrade never connects to a database.
- Fail closed: refuse to run on a dirty working tree or while any `*.template-new` file exists; never overwrite a locally modified file.
- Downstream projects do not share Git history with the template (GitHub "Use this template"), so upgrades are file-level, never `git merge`.
- Do not commit or push without an explicit instruction (AGENTS.md).

---

## File Structure

| Path | Owner | Responsibility |
| --- | --- | --- |
| `template-manifest.json` | template | Ownership globs and upstream URL |
| `AGENTS.project.md` | project placeholder | Project-specific agent instructions |
| `PROJECT.md` | project placeholder | Project overview for humans |
| `.agents/rules/project.md` | project placeholder | Project-specific agent rules |
| `CLAUDE.md` | template | Imports `AGENTS.md` then `AGENTS.project.md` for Claude Code |
| `AGENTS.md` | template | Points agents at the project placeholders |
| `scripts/upgrade_template.py` | template | Upgrade engine and CLI |
| `scripts/team.sh`, `scripts/team.ps1` | template | `upgrade-template` command |
| `tests/test_template_manifest.py` | template | Every template/placeholder path is classified once; downstream app, migration, environment, and lock paths stay unowned |
| `tests/test_upgrade_template.py` | template | Engine behavior against real temporary Git repos |
| `README.md` | template | "Upgrading from the template" section |
| `.template-lock.json` | written in downstream projects only | Installed template commit and per-file hashes |

---

### Task 1: Ownership manifest and project placeholders

**Files:**
- Create: `template-manifest.json`, `AGENTS.project.md`, `PROJECT.md`, `.agents/rules/project.md`, `CLAUDE.md`, `tests/test_template_manifest.py`
- Modify: `AGENTS.md` (new "Project instructions" section), `tests/test_documentation_contract.py` (add `CLAUDE.md` to `DOCS`)

**Interfaces:**
- Produces: `template-manifest.json` with keys `schemaVersion` (int, `1`), `upstream` (str, Git URL), `templateOwned` (list of globs), `projectOwned` (list of exact paths), `templateOnly` (list of globs). Glob rules used by Task 2: `**` matches any number of path segments including none, `*` matches within one segment, paths use `/`. In a downstream clone, application source, database mirrors, migrations, `.env`, and `.template-lock.json` are intentionally unowned; they are not expected to be classified as template files.

- [x] **Step 1: Write the failing classification test**

Create `tests/test_template_manifest.py`:

```python
import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    # Mirrors scripts/upgrade_template.py so the test does not import it.
    parts = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(parts) + "$")


class TemplateManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((ROOT / "template-manifest.json").read_text(encoding="utf-8"))
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
        ).stdout.decode("utf-8")
        self.tracked = [path for path in tracked.split("\0") if path]

    def classify(self, path: str) -> list[str]:
        owners = []
        if any(glob_to_regex(p).match(path) for p in self.manifest["templateOwned"]):
            owners.append("templateOwned")
        if path in self.manifest["projectOwned"]:
            owners.append("projectOwned")
        if any(glob_to_regex(p).match(path) for p in self.manifest["templateOnly"]):
            owners.append("templateOnly")
        return owners

    def test_every_tracked_file_has_exactly_one_owner(self) -> None:
        for path in self.tracked:
            with self.subTest(path=path):
                self.assertEqual(len(self.classify(path)), 1, self.classify(path))

    def test_project_placeholders_exist_and_are_not_template_owned(self) -> None:
        for path in self.manifest["projectOwned"]:
            with self.subTest(path=path):
                self.assertIn(path, self.tracked)
                self.assertEqual(self.classify(path), ["projectOwned"])

    def test_project_data_is_never_managed(self) -> None:
        for path in (
            "apps/DEMO/100/application.apx",
            "database/DEMO/tables/T.sql",
            "migrations/alice/20260926_add.sql",
            "app_context/100/purpose.md",
            ".env",
            ".template-lock.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), [])

    def test_agent_entry_points_import_project_instructions(self) -> None:
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("@AGENTS.md", claude)
        self.assertIn("@AGENTS.project.md", claude)
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("AGENTS.project.md", agents)
        self.assertIn(".agents/rules/project.md", agents)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run it and confirm it fails**

Run: `python3 -m unittest tests.test_template_manifest -v`
Expected: ERROR, `FileNotFoundError: ... template-manifest.json`.

- [x] **Step 3: Create the manifest**

Create `template-manifest.json`:

```json
{
  "schemaVersion": 1,
  "upstream": "https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM.git",
  "templateOwned": [
    ".agents/rules/agent-safety.md",
    ".agents/rules/graphify.md",
    ".agents/workflows/**",
    ".agents/skills/**",
    ".claude/skills/**",
    ".env.example",
    ".gitattributes",
    ".github/**",
    ".gitignore",
    ".graphifyignore",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "app_context/README.md",
    "apps/templates/**",
    "docs/publish-rules.md",
    "migrations/README.md",
    "pyproject.toml",
    "scripts/**",
    "self_improve.md",
    "template-manifest.json",
    "tests/**"
  ],
  "projectOwned": [
    ".agents/rules/project.md",
    "AGENTS.project.md",
    "PROJECT.md",
    "ai_generate/.gitkeep",
    "apps/.gitkeep",
    "database/.gitkeep",
    "migrations/.gitkeep"
  ],
  "templateOnly": [
    "docs/superpowers/**"
  ]
}
```

- [x] **Step 4: Create the empty placeholders**

Each placeholder holds only an HTML comment, so it renders empty and tells a reader what belongs there.

`AGENTS.project.md`:

```markdown
<!-- Project-specific agent instructions. The template upgrade never overwrites this file. -->
```

`PROJECT.md`:

```markdown
<!-- Project overview for this team. The template upgrade never overwrites this file. -->
```

`.agents/rules/project.md`:

```markdown
<!-- Project-specific agent rules. The template upgrade never overwrites this file. -->
```

`CLAUDE.md` (Claude Code reads `CLAUDE.md`, not `AGENTS.md`; `@path` lines import files):

```markdown
@AGENTS.md
@AGENTS.project.md
```

- [x] **Step 5: Point AGENTS.md at the placeholders**

In `AGENTS.md`, insert this section directly before `## Rules for coding agents`:

```markdown
## Project instructions

- `AGENTS.md`, `CLAUDE.md`, `README.md`, and `.agents/rules/agent-safety.md`
  belong to the template; `scripts/team.sh upgrade-template` replaces them.
  Put project-specific agent instructions in `AGENTS.project.md`, project
  rules in `.agents/rules/project.md`, and the project overview in
  `PROJECT.md`. The upgrade never overwrites those files.
- Read `AGENTS.project.md` and `.agents/rules/project.md` after this file.
  When they conflict with this file, ask the user which applies.
```

In `tests/test_documentation_contract.py`, add `ROOT / "CLAUDE.md",` to the `DOCS` tuple after `ROOT / "AGENTS.md",`.

- [x] **Step 6: Run the tests**

Run: `git add -N template-manifest.json AGENTS.project.md PROJECT.md .agents/rules/project.md CLAUDE.md tests/test_template_manifest.py && python3 -m unittest tests.test_template_manifest tests.test_documentation_contract -v`
Expected: PASS. (`git add -N` makes the new files visible to `git ls-files` before the commit.) The template checkout's eligible files must have exactly one owner. A downstream-repository fixture must also prove its app source, migration, `.env`, and lock remain unowned. Do not widen `templateOwned` to `**` to silence ownership failures.

- [x] **Step 7: Run the full suite and commit**

Run: `python3 -m unittest discover -s tests`
Expected: `OK`.

```bash
git add template-manifest.json AGENTS.project.md PROJECT.md .agents/rules/project.md CLAUDE.md AGENTS.md tests/test_template_manifest.py tests/test_documentation_contract.py
git commit -m "feat: declare template file ownership and project instruction placeholders"
```

---

### Task 2: Upgrade engine

**Files:**
- Create: `scripts/upgrade_template.py`, `tests/test_upgrade_template.py`

**Interfaces:**
- Consumes: `template-manifest.json` schema from Task 1.
- Produces (used by Task 3):
  - CLI: `python3 scripts/upgrade_template.py --project-root <dir> [--source <git-url-or-path>] [--ref <ref>] [--dry-run]`. Resolve source in this order: explicit `--source`, upstream in the target's `.template-lock.json`, then upstream in the target's `template-manifest.json`. This supports a new GitHub template clone that has no lock yet.
  - Exit codes: `0` applied (or dry run) with nothing needing attention; `1` applied with conflicts or kept files the user must review; `2` refused or failed. Filesystem writes are rolled back when possible; if rollback is incomplete, keep and use the reported recovery directory before retrying.
  - Output lines: `<ACTION> <path>` where ACTION is one of `CREATE`, `UPDATE`, `UNCHANGED`, `KEEP-LOCAL`, `CONFLICT`, `DELETE`, `KEEP-DELETED`, `KEEP-REMOVED`, `PLACEHOLDER`, `KEEP-PLACEHOLDER`, then `Template <commit> <summary>`.
  - Writes `.template-lock.json`: `{"schemaVersion": 1, "upstream": str, "commit": str, "files": {path: sha256_hex}}` covering template-owned files only.

Decision table the engine implements, per template-owned path (`N` new template hash, `L` lock hash or none, `C` local hash or none):

| Local | Condition | Action | Lock records |
| --- | --- | --- | --- |
| missing | `L` none | `CREATE` | `N` |
| missing | `L` present | `KEEP-DELETED` (report; do not restore) | `N` |
| present | `C == N` | `UNCHANGED` | `N` |
| present | `C == L` | `UPDATE` (overwrite with new template) | `N` |
| present | `N == L` | `KEEP-LOCAL` (customized; template unchanged) | `N` |
| present | otherwise | `CONFLICT` (write `<path>.template-new`) | `N` |

Paths in the lock but no longer in the template: `DELETE` when `C == L`, `KEEP-REMOVED` when modified, silently dropped from the lock when already missing. Project-owned paths: `PLACEHOLDER` when missing, else `KEEP-PLACEHOLDER`. A project with no lock (every project created before this feature) has `L` none everywhere, so any differing file becomes a `CONFLICT` rather than being overwritten.

- [x] **Step 1: Write the failing engine tests**

Create `tests/test_upgrade_template.py`:

```python
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "scripts" / "upgrade_template.py"
MANIFEST = {
    "schemaVersion": 1,
    "upstream": "unused",
    "templateOwned": ["AGENTS.md", "scripts/**", "template-manifest.json"],
    "projectOwned": ["AGENTS.project.md"],
    "templateOnly": ["docs/**"],
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")


def write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def commit_all(root: Path, message: str) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


class UpgradeTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.template = base / "template"
        self.project = base / "project"
        init_repo(self.template)
        write(
            self.template,
            {
                "template-manifest.json": json.dumps(MANIFEST),
                "AGENTS.md": "rules v1\n",
                "AGENTS.project.md": "<!-- placeholder -->\n",
                "scripts/tool.sh": "echo v1\n",
                "scripts/old.sh": "echo old\n",
                "docs/plan.md": "template only\n",
            },
        )
        commit_all(self.template, "v1")
        # A project created from v1 by copying files (no shared history).
        init_repo(self.project)
        write(
            self.project,
            {
                "AGENTS.md": "rules v1\n",
                "AGENTS.project.md": "our project rules\n",
                "scripts/tool.sh": "echo v1\n",
                "scripts/old.sh": "echo old\n",
                "apps/DEMO/100/application.apx": "app X ()\n",
            },
        )
        commit_all(self.project, "created from template")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def upgrade(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(ENGINE), "--project-root", str(self.project), "--source", str(self.template), *extra],
            text=True,
            capture_output=True,
            check=False,
        )

    def release_v2(self, files: dict[str, str], removed: tuple[str, ...] = ()) -> None:
        write(self.template, files)
        for relative in removed:
            (self.template / relative).unlink()
        commit_all(self.template, "v2")

    def read(self, relative: str) -> str:
        return (self.project / relative).read_text(encoding="utf-8")

    def lock(self) -> dict:
        return json.loads(self.read(".template-lock.json"))

    def adopt(self) -> None:
        # First upgrade of an identical copy: everything UNCHANGED, lock written.
        result = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commit_all(self.project, "adopt template lock")

    def test_first_upgrade_of_identical_copy_writes_lock_without_changes(self) -> None:
        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UNCHANGED AGENTS.md", result.stdout)
        self.assertEqual(self.lock()["commit"], git(self.template, "rev-parse", "HEAD"))
        self.assertIn("scripts/tool.sh", self.lock()["files"])
        self.assertNotIn("AGENTS.project.md", self.lock()["files"])

    def test_unmodified_template_files_are_updated_and_new_files_created(self) -> None:
        self.adopt()
        self.release_v2({"scripts/tool.sh": "echo v2\n", "scripts/new.sh": "echo new\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UPDATE scripts/tool.sh", result.stdout)
        self.assertIn("CREATE scripts/new.sh", result.stdout)
        self.assertEqual(self.read("scripts/tool.sh"), "echo v2\n")
        self.assertEqual(self.read("scripts/new.sh"), "echo new\n")

    def test_locally_modified_file_changed_upstream_is_a_conflict(self) -> None:
        self.adopt()
        write(self.project, {"AGENTS.md": "rules v1 plus ours\n"})
        commit_all(self.project, "customize")
        self.release_v2({"AGENTS.md": "rules v2\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("CONFLICT AGENTS.md", result.stdout)
        self.assertEqual(self.read("AGENTS.md"), "rules v1 plus ours\n")
        self.assertEqual(self.read("AGENTS.md.template-new"), "rules v2\n")

    def test_resolved_conflict_is_not_reported_again(self) -> None:
        self.adopt()
        write(self.project, {"AGENTS.md": "rules v1 plus ours\n"})
        commit_all(self.project, "customize")
        self.release_v2({"AGENTS.md": "rules v2\n"})
        self.upgrade()
        write(self.project, {"AGENTS.md": "rules v2 plus ours\n"})
        (self.project / "AGENTS.md.template-new").unlink()
        commit_all(self.project, "merge template v2")

        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("KEEP-LOCAL AGENTS.md", result.stdout)
        self.assertEqual(self.read("AGENTS.md"), "rules v2 plus ours\n")

    def test_customized_file_unchanged_upstream_is_kept(self) -> None:
        self.adopt()
        write(self.project, {"scripts/tool.sh": "echo ours\n"})
        commit_all(self.project, "customize")
        self.release_v2({"AGENTS.md": "rules v2\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("KEEP-LOCAL scripts/tool.sh", result.stdout)
        self.assertEqual(self.read("scripts/tool.sh"), "echo ours\n")

    def test_project_placeholders_are_never_overwritten_but_are_created_when_missing(self) -> None:
        self.release_v2({"AGENTS.project.md": "<!-- placeholder v2 -->\n"})
        result = self.upgrade()
        self.assertIn("KEEP-PLACEHOLDER AGENTS.project.md", result.stdout)
        self.assertEqual(self.read("AGENTS.project.md"), "our project rules\n")

        (self.project / "AGENTS.project.md").unlink()
        commit_all(self.project, "remove placeholder")
        result = self.upgrade()
        self.assertIn("PLACEHOLDER AGENTS.project.md", result.stdout)
        self.assertEqual(self.read("AGENTS.project.md"), "<!-- placeholder v2 -->\n")

    def test_files_removed_upstream_are_deleted_only_when_unmodified(self) -> None:
        self.adopt()
        write(self.project, {"scripts/tool.sh": "echo ours\n"})
        commit_all(self.project, "customize")
        self.release_v2({}, removed=("scripts/old.sh", "scripts/tool.sh"))

        result = self.upgrade()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("DELETE scripts/old.sh", result.stdout)
        self.assertIn("KEEP-REMOVED scripts/tool.sh", result.stdout)
        self.assertFalse((self.project / "scripts/old.sh").exists())
        self.assertEqual(self.read("scripts/tool.sh"), "echo ours\n")

    def test_project_data_and_template_only_files_are_untouched(self) -> None:
        self.release_v2({"docs/plan.md": "template only v2\n"})

        self.upgrade()

        self.assertEqual(self.read("apps/DEMO/100/application.apx"), "app X ()\n")
        self.assertFalse((self.project / "docs").exists())

    def test_first_upgrade_never_overwrites_a_differing_file(self) -> None:
        write(self.project, {"AGENTS.md": "rules edited before any lock\n"})
        commit_all(self.project, "edit")

        result = self.upgrade()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("CONFLICT AGENTS.md", result.stdout)
        self.assertEqual(self.read("AGENTS.md"), "rules edited before any lock\n")

    def test_dirty_tree_or_pending_conflict_file_is_refused(self) -> None:
        write(self.project, {"scratch.txt": "uncommitted\n"})
        result = self.upgrade()
        self.assertEqual(result.returncode, 2)
        self.assertIn("uncommitted changes", result.stderr)
        (self.project / "scratch.txt").unlink()

        write(self.project, {"AGENTS.md.template-new": "left over\n"})
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "oops")
        result = self.upgrade()
        self.assertEqual(result.returncode, 2)
        self.assertIn(".template-new", result.stderr)

    def test_dry_run_changes_nothing(self) -> None:
        self.adopt()
        self.release_v2({"scripts/tool.sh": "echo v2\n"})

        result = self.upgrade("--dry-run")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UPDATE scripts/tool.sh", result.stdout)
        self.assertEqual(self.read("scripts/tool.sh"), "echo v1\n")
        self.assertEqual(git(self.project, "status", "--porcelain"), "")

    def test_ref_selects_the_template_version(self) -> None:
        v1 = git(self.template, "rev-parse", "HEAD")
        self.release_v2({"scripts/tool.sh": "echo v2\n"})

        result = self.upgrade("--ref", v1)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read("scripts/tool.sh"), "echo v1\n")
        self.assertEqual(self.lock()["commit"], v1)

    @unittest.skipIf(os.name == "nt", "symbolic links need privileges on Windows")
    def test_symbolic_link_in_template_is_refused(self) -> None:
        (self.template / "scripts/link.sh").symlink_to("tool.sh")
        commit_all(self.template, "link")

        result = self.upgrade()

        self.assertEqual(result.returncode, 2)
        self.assertIn("symbolic link", result.stderr)
        self.assertFalse((self.project / "scripts/link.sh").exists())


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run the tests and confirm they fail**

Run: `python3 -m unittest tests.test_upgrade_template -v`
Expected: every test fails with `can't open file '.../scripts/upgrade_template.py'`.

- [x] **Step 3: Implement the engine**

Create `scripts/upgrade_template.py`:

```python
#!/usr/bin/env python3
"""Upgrade a project created from the APEX team template to a newer template.

Only paths named by the template's template-manifest.json are touched.
Template-owned files are updated when the local copy still matches the last
installed template; customized files are kept, and a real conflict leaves the
new version beside the local file as <path>.template-new. Project-owned
placeholder files are created when missing and never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


MANIFEST_NAME = "template-manifest.json"
LOCK_NAME = ".template-lock.json"
CONFLICT_SUFFIX = ".template-new"
ATTENTION = {"CONFLICT", "KEEP-DELETED", "KEEP-REMOVED"}


class UpgradeError(Exception):
    """A refusal or failure that leaves the project unchanged."""


@dataclass(frozen=True)
class Action:
    kind: str
    path: str


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    parts = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(parts) + "$")


def run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise UpgradeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_template(source: str, ref: str | None, destination: Path) -> str:
    run_git(destination.parent, "clone", "--quiet", source, str(destination))
    if ref:
        run_git(destination, "checkout", "--quiet", "--detach", ref)
    return run_git(destination, "rev-parse", "HEAD").strip()


def load_manifest(template_root: Path) -> dict:
    path = template_root / MANIFEST_NAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpgradeError(f"template manifest is missing or invalid: {exc}") from exc
    if manifest.get("schemaVersion") != 1:
        raise UpgradeError("unsupported template manifest schemaVersion; upgrade this script first")
    for key in ("templateOwned", "projectOwned", "templateOnly"):
        if not isinstance(manifest.get(key), list):
            raise UpgradeError(f"template manifest key {key} must be a list")
    return manifest


def tracked_template_files(template_root: Path) -> dict[str, bool]:
    """Map each tracked path to whether it is a symbolic link (mode 120000)."""
    listing = run_git(template_root, "ls-files", "-s", "-z")
    files = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        files[path] = meta.split()[0] == "120000"
    return files


def classify(template_root: Path, manifest: dict) -> tuple[list[str], list[str]]:
    owned_patterns = [glob_to_regex(p) for p in manifest["templateOwned"]]
    project_owned = set(manifest["projectOwned"])
    template_owned, placeholders = [], []
    for path, is_link in sorted(tracked_template_files(template_root).items()):
        managed = path in project_owned or any(p.match(path) for p in owned_patterns)
        if managed and is_link:
            raise UpgradeError(f"template contains a symbolic link, which is not supported: {path}")
        if path in project_owned:
            placeholders.append(path)
        elif any(p.match(path) for p in owned_patterns):
            template_owned.append(path)
    return template_owned, placeholders


def read_lock(project_root: Path) -> dict:
    path = project_root / LOCK_NAME
    if not path.exists():
        return {"files": {}}
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpgradeError(f"{LOCK_NAME} is invalid: {exc}") from exc
    if not isinstance(lock.get("files"), dict):
        raise UpgradeError(f"{LOCK_NAME} has no files map")
    return lock


def check_project(project_root: Path) -> None:
    if run_git(project_root, "status", "--porcelain", "--untracked-files=all").strip():
        raise UpgradeError("the project has uncommitted changes; commit or stash them before upgrading")
    pending = [p for p in run_git(project_root, "ls-files", "-z").split("\0") if p.endswith(CONFLICT_SUFFIX)]
    if pending:
        raise UpgradeError(f"resolve and delete {CONFLICT_SUFFIX} files first: {', '.join(pending)}")


def plan_upgrade(
    project_root: Path, template_root: Path, template_owned: list[str], placeholders: list[str], lock: dict
) -> tuple[list[Action], dict[str, str]]:
    actions: list[Action] = []
    new_lock: dict[str, str] = {}
    installed = lock["files"]
    for path in template_owned:
        new = sha256(template_root / path)
        local = sha256(project_root / path)
        last = installed.get(path)
        new_lock[path] = new
        if local is None:
            kind = "CREATE" if last is None else "KEEP-DELETED"
        elif local == new:
            kind = "UNCHANGED"
        elif local == last:
            kind = "UPDATE"
        elif new == last:
            kind = "KEEP-LOCAL"
        else:
            kind = "CONFLICT"
        actions.append(Action(kind, path))
    for path, last in sorted(installed.items()):
        if path in new_lock:
            continue
        local = sha256(project_root / path)
        if local is None:
            continue
        actions.append(Action("DELETE" if local == last else "KEEP-REMOVED", path))
    for path in placeholders:
        exists = (project_root / path).exists()
        actions.append(Action("KEEP-PLACEHOLDER" if exists else "PLACEHOLDER", path))
    return actions, new_lock


# apply_actions stages every managed write, conflict file, and updated lock in a
# project-local scratch directory before changing targets. It rejects protected
# paths, symlinked parents, and non-regular targets. Replaced or deleted originals
# move into scratch before staged files are installed with os.replace. If an
# operation fails, reverse completed operations in reverse order. Remove scratch
# only after a complete rollback; if rollback fails, retain the recovery backups
# and report their path so the operator can restore them manually.


def write_lock(project_root: Path, upstream: str, commit: str, files: dict[str, str]) -> None:
    payload = {"schemaVersion": 1, "upstream": upstream, "commit": commit, "files": dict(sorted(files.items()))}
    with (project_root / LOCK_NAME).open("w", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(payload, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--source", help="template Git URL or local path (default: lock upstream, then manifest upstream)")
    parser.add_argument("--ref", help="template branch, tag, or commit (default: the source's default branch)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan without changing files")
    args = parser.parse_args(argv)

    try:
        project_root = Path(run_git(args.project_root, "rev-parse", "--show-toplevel").strip())
        lock = read_lock(project_root)
        source = args.source or lock.get("upstream") or load_manifest(project_root)["upstream"]
        if not source:
            raise UpgradeError("no template source recorded in lock or manifest; pass --source <template Git URL or path>")
        if not args.dry_run:
            check_project(project_root)
        with tempfile.TemporaryDirectory(prefix="apex-template-") as temporary:
            template_root = Path(temporary) / "template"
            commit = fetch_template(source, args.ref, template_root)
            manifest = load_manifest(template_root)
            template_owned, placeholders = classify(template_root, manifest)
            actions, new_lock = plan_upgrade(project_root, template_root, template_owned, placeholders, lock)
            for action in actions:
                print(f"{action.kind} {action.path}")
            if not args.dry_run:
                apply_actions(project_root, template_root, actions)
                write_lock(project_root, source, commit, new_lock)
    except UpgradeError as exc:
        print(f"template upgrade error: {exc}", file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    for action in actions:
        counts[action.kind] = counts.get(action.kind, 0) + 1
    summary = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
    print(f"Template {commit} {summary}{' (dry run)' if args.dry_run else ''}")
    attention = [a for a in actions if a.kind in ATTENTION]
    if attention and not args.dry_run:
        print(
            f"Review {len(attention)} file(s): merge each {CONFLICT_SUFFIX} into its file and delete it; "
            "KEEP-* files were left as they are.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 4: Run the tests and confirm they pass**

Run: `python3 -m unittest tests.test_upgrade_template -v`
Expected: all tests PASS.

- [x] **Step 5: Lint, run the full suite, and commit**

Run: `.venv/bin/ruff check scripts tests && python3 -m unittest discover -s tests`
Expected: `All checks passed!` and `OK`.

```bash
git add scripts/upgrade_template.py tests/test_upgrade_template.py
git commit -m "feat: add file-level template upgrade engine"
```

---

### Task 3: `upgrade-template` command in both team CLIs

**Files:**
- Modify: `scripts/team.sh` (usage text and a new `upgrade-template)` case), `scripts/team.ps1` (usage text and a new `"upgrade-template"` case)
- Test: `tests/test_team_cli.py`

**Interfaces:**
- Consumes: `scripts/upgrade_template.py` CLI and exit codes from Task 2.
- Produces: `scripts/team.sh upgrade-template [--source <url-or-path>] [--ref <ref>] [--dry-run]` and the same for `scripts/team.ps1`. It passes the checkout root as `--project-root` and returns the engine's exit code. After a non-dry-run upgrade it runs the Bash `.env` loader against `.env` when present and prints `warning: .env needs attention after the upgrade:` plus the loader's message if it fails; the upgrade's exit code is unchanged by that warning.

- [x] **Step 1: Write the failing CLI tests**

Append these methods to `TeamCliTests` in `tests/test_team_cli.py`:

```python
    def make_upgrade_fixture(self, root: Path) -> tuple[Path, Path]:
        template = root / "template"
        project = root / "project"
        for repo in (template, project):
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (template / "template-manifest.json").write_text(
            '{"schemaVersion": 1, "upstream": "x", "templateOwned": ["scripts/**", "template-manifest.json"],'
            ' "projectOwned": [], "templateOnly": []}',
            encoding="utf-8",
        )
        (template / "scripts").mkdir()
        (template / "scripts" / "hello.sh").write_text("echo template\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(template), "commit", "-q", "-m", "v1"], check=True)
        (project / "scripts").mkdir()
        for name in ("team.sh", "team.ps1", "upgrade_template.py", "load_env.sh", "load_env.ps1"):
            shutil.copy2(ROOT / "scripts" / name, project / "scripts" / name)
        # .env is local configuration; ignoring it keeps the tree clean for the engine.
        (project / ".gitignore").write_text(".env\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-q", "-m", "project"], check=True)
        return template, project

    def test_upgrade_template_command_runs_the_engine_for_this_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template, project = self.make_upgrade_fixture(Path(temporary))

            result = subprocess.run(
                ["bash", str(project / "scripts" / "team.sh"), "upgrade-template", "--source", str(template)],
                cwd=project, text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("CREATE scripts/hello.sh", result.stdout)
            self.assertTrue((project / ".template-lock.json").is_file())

    def test_upgrade_template_warns_when_env_misses_a_new_required_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template, project = self.make_upgrade_fixture(Path(temporary))
            (project / ".env").write_text("PROJECT_NAME=x\n", encoding="utf-8")

            result = subprocess.run(
                ["bash", str(project / "scripts" / "team.sh"), "upgrade-template", "--source", str(template)],
                cwd=project, text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(".env needs attention after the upgrade", result.stderr)

    def test_powershell_upgrade_template_command_runs_the_engine(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            template, project = self.make_upgrade_fixture(Path(temporary))

            result = subprocess.run(
                [pwsh, "-NoProfile", "-File", str(project / "scripts" / "team.ps1"), "upgrade-template",
                 "--source", str(template), "--dry-run"],
                cwd=project, text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("CREATE scripts/hello.sh", result.stdout)
            self.assertFalse((project / ".template-lock.json").exists())
```

Also add `"upgrade-template"` to the command tuple in `test_team_help_lists_primary_commands`.

- [x] **Step 2: Run the tests and confirm they fail**

Run: `python3 -m unittest tests.test_team_cli -v`
Expected: the three new tests and the help test FAIL (`unknown command 'upgrade-template'`).

- [x] **Step 3: Add the Bash command**

In `scripts/team.sh`, add this usage line after the `deploy` lines:

```text
  upgrade-template [--source <url|path>] [--ref <ref>] [--dry-run]
                                              Update template-owned files from the template
```

and add this case before `*)`:

```bash
  upgrade-template)
    python3 "$REPO_ROOT/scripts/upgrade_template.py" --project-root "$REPO_ROOT" "$@" || upgrade_status=$?
    upgrade_status="${upgrade_status:-0}"
    dry_run=false
    for upgrade_argument in "$@"; do [ "$upgrade_argument" != --dry-run ] || dry_run=true; done
    if [ "$upgrade_status" -ne 2 ] && [ "$dry_run" = false ] && [ -f "$REPO_ROOT/.env" ]; then
      if ! env_check="$(bash -c 'source "$1" "$2"' bash "$REPO_ROOT/scripts/load_env.sh" "$REPO_ROOT/.env" 2>&1)"; then
        printf 'warning: .env needs attention after the upgrade:\n%s\n' "$env_check" >&2
      fi
    fi
    exit "$upgrade_status"
    ;;
```

- [x] **Step 4: Add the PowerShell command**

In `scripts/team.ps1`, add the same usage lines to `Show-Usage` and this case before `default`:

```powershell
  "upgrade-template" {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
    if ($null -eq $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
    if ($null -eq $python) { throw "Python 3 is required to upgrade the template" }
    $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    & $python.Source (Join-Path $PSScriptRoot "upgrade_template.py") --project-root $repoRoot @Arguments
    $upgradeStatus = $LASTEXITCODE
    $envFile = Join-Path $repoRoot ".env"
    if ($upgradeStatus -ne 2 -and $Arguments -notcontains "--dry-run" -and (Test-Path -LiteralPath $envFile)) {
      try {
        . (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $envFile
      } catch {
        Write-Warning ".env needs attention after the upgrade: $($_.Exception.Message)"
      }
    }
    exit $upgradeStatus
  }
```

- [x] **Step 5: Run the tests and confirm they pass**

Run: `python3 -m unittest tests.test_team_cli -v && shellcheck -S warning scripts/team.sh`
Expected: PASS and no shellcheck warnings.

- [x] **Step 6: Commit**

```bash
git add scripts/team.sh scripts/team.ps1 tests/test_team_cli.py
git commit -m "feat: add upgrade-template to the team CLIs"
```

---

### Task 4: Upgrade documentation, including projects created before the manifest

**Files:**
- Modify: `README.md` (new section before `## Command reference`, one table row), `tests/test_documentation_contract.py`

**Interfaces:**
- Consumes: CLI from Task 3; ownership rules from Task 1.

- [x] **Step 1: Write the failing documentation assertions**

In `tests/test_documentation_contract.py`, inside `test_active_team_guidance_uses_the_current_cli`, append:

```python
        self.assertIn("scripts/team.sh upgrade-template", readme)
        self.assertIn("AGENTS.project.md", readme)
        self.assertIn(".template-new", readme)
        self.assertIn("python3 /tmp/apex-template/scripts/upgrade_template.py", readme)
```

Run: `python3 -m unittest tests.test_documentation_contract -v`
Expected: FAIL.

- [x] **Step 2: Add the README section**

Insert before `## Command reference`:

````markdown
## Upgrading from the template

Projects created from this template do not share its Git history, so updates
are copied file by file. `template-manifest.json` decides what the upgrade may
touch: template scripts, tests, CI, and agent guidance are upgraded; project
files such as `AGENTS.project.md`, `.agents/rules/project.md`, and
`PROJECT.md` are created once and never overwritten; application source,
database mirrors, migrations, `app_context/<id>/`, and `.env` are never
touched. Put project-specific instructions in those placeholder files, not in
`AGENTS.md` or `README.md`.

Commit your work, then run:

```bash
scripts/team.sh upgrade-template --dry-run
scripts/team.sh upgrade-template
git status
```

A file you customized is kept when the template did not change it. When both
changed, the upgrade keeps your file, writes the new template version beside
it as `<file>.template-new`, and exits with status 1: merge the two, delete the
`.template-new` file, and commit. The next upgrade refuses to run while any
`.template-new` file remains. The upgrade records the installed template commit
in `.template-lock.json`; commit it with the upgraded files. Use `--ref <tag>`
to install a specific template version and `--source <url>` for a fork. If the
upgrade reports that `.env` needs attention, compare it with `.env.example`.

Projects created before `template-manifest.json` existed do not have the
upgrade script yet. Run it once from a fresh template clone:

```bash
git clone https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM.git /tmp/apex-template
python3 /tmp/apex-template/scripts/upgrade_template.py --project-root . --source https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM.git
```

That first run has no lock, so every file that differs from the template is
reported as a conflict instead of being overwritten. With no explicit source
or lock, the script uses the upstream URL in the target's manifest. An explicit
`--source` overrides the lock, and a lock overrides the manifest.
````

Add this row to the command table:

```markdown
| `scripts/team.sh upgrade-template [--dry-run]` | Update template-owned files; never overwrites project files. |
```

- [x] **Step 3: Run the tests and commit**

Run: `python3 -m unittest discover -s tests`
Expected: `OK`.

```bash
git add README.md tests/test_documentation_contract.py
git commit -m "docs: explain template upgrades and project placeholder files"
```

---

### Task 5: End-to-end dogfood on a copy made from an older template commit

**Files:** none committed. Work only under the session scratchpad (`$SCRATCH` below).

**Interfaces:**
- Consumes: everything above on `main`.

- [x] **Step 1: Create a project the way a developer did before this feature**

Run from the template checkout. Set `SCRATCH` to the session scratchpad
directory printed in the agent's environment.

```bash
REPO="$(git rev-parse --show-toplevel)"
rm -rf "$SCRATCH/upgrade-e2e" && mkdir -p "$SCRATCH/upgrade-e2e"
git -C "$REPO" archive 2dc1625 | tar -x -C "$SCRATCH/upgrade-e2e"
cd "$SCRATCH/upgrade-e2e"
git init -q -b main && git add -A && git commit -q -m "project created from template 2dc1625"
printf 'Our team rule.\n' >> AGENTS.md && git commit -qam "customize AGENTS.md"
```

`2dc1625` is the last template commit before the manifest; it has no `upgrade_template.py`.

- [x] **Step 2: Bootstrap the upgrade from the current template**

```bash
python3 "$REPO/scripts/upgrade_template.py" --project-root . --source "$REPO"
echo "exit=$?"
git status --short
```

Expected: exit `1`. `CONFLICT AGENTS.md` with `AGENTS.md.template-new` present. `CREATE` lines for `template-manifest.json`, `CLAUDE.md`, and `scripts/upgrade_template.py`. `PLACEHOLDER AGENTS.project.md`, `PLACEHOLDER PROJECT.md`, `PLACEHOLDER .agents/rules/project.md`. `CREATE docs/publish-rules.md` (added after `2dc1625`), and no `docs/superpowers/` files. `AGENTS.md` still ends with `Our team rule.`

- [x] **Step 3: Resolve and upgrade again through the team CLI**

```bash
mv AGENTS.md.template-new AGENTS.md && printf 'Our team rule.\n' >> AGENTS.project.md
git add -A && git commit -q -m "adopt template upgrade"
scripts/team.sh upgrade-template --source "$REPO"
echo "exit=$?"
```

Expected: exit `0`, only `UNCHANGED`/`KEEP-PLACEHOLDER` lines, and `git status --short` empty except `.template-lock.json` when its commit changed.

- [x] **Step 4: Record the evidence**

Report the exit codes and action lines from Steps 2 and 3 to the user. Do not commit anything from `$SCRATCH`.

---

## Self-Review Notes

- Spec coverage: upgrade scripts and instructions (Tasks 2–3), never overwrite project instruction files (Task 1 ownership, Task 2 `KEEP-PLACEHOLDER`), empty placeholder files in the template (Task 1), existing downstream projects (Task 4 bootstrap, Task 5 dogfood).
- Out of scope: merging customized files automatically (the engine writes `.template-new` instead), and upgrading `docs/superpowers/` planning history (`templateOnly`). `docs/publish-rules.md` is template-owned so projects receive it.

## Post-plan review fixes

Follow-up review fixes on `codex/two-plans-and-superpowers` resolve an
unlocked-clone bootstrap from the target manifest, retain filesystem recovery
backups when rollback is incomplete, and allow committed downstream app,
migration, `.env`, and lock files to remain outside template ownership. The
README documents the source precedence and recovery directory. Regression
tests cover each case.

The original plan verification ran 176 tests and passed;
`.venv/bin/ruff check .agents tests scripts`, `shellcheck -S warning` for the
project and probe scripts, Bash syntax validation, and `git diff --check` also
passed. After review hardening, `bash -n` and `git diff --check` passed. The
user has explicitly authorized committing and pushing the follow-up changes to
`main` as part of the integration changeset.
