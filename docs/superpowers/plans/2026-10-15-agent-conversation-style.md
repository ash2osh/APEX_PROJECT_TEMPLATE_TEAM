# Agent Conversation Style Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a developer choose how coding agents talk and present decisions through `.env` (`AGENT_STYLE`), with four template styles and a learning layer (`personal.md`) on top.

**Architecture:** Two new `.env` keys are validated identically in `local_config.py`, `load_env.sh` and `load_env.ps1`. Four template-owned style files and one project-owned `personal.md` placeholder live in `.agents/styles/`. `AGENTS.md` tells every agent to load them at the start of a conversation. The 26.1 change is built once, then ported to 26.2 as a patch.

**Tech Stack:** Python 3.10 `unittest`, Bash, PowerShell, Markdown, `template-manifest.json`, GitHub Actions (`offline`, `windows-offline`, `macos-installer`), `ruff`.

**Spec:** `docs/superpowers/specs/2026-10-15-agent-conversation-style-design.md`

## Global Constraints

- Default style is `standard`; valid values exactly `standard`, `concise`, `teaching`, `asharif-style` (lowercase). `AGENT_STYLE_LEARNING` is `on` or `off`, default `on`.
- Error messages: `AGENT_STYLE must be one of: standard, concise, teaching, asharif-style` and `AGENT_STYLE_LEARNING must be on or off`. Errors never print the value.
- The three loaders must agree. Existing keys are strict; empty or wrong-case values are errors, not defaults.
- Style files: at most 70 lines each, the same eight sections, the exact "never overrides" sentence, no URLs, hosts, e-mail addresses, schema or project names. `personal.md`: at most 40 lines.
- A path has one owner: the five style files are `templateOwned` by exact path; `.agents/styles/personal.md` is `projectOwned`.
- Files use LF line endings. No credentials anywhere.
- Commits, pushes and PRs happen only in the two template clones, on branch `feat/agent-conversation-style`, under this plan's approval. Merge with merge commits (no squash, no branch deletion). Nothing is committed in `natrec_team`.

## Review Focus

1. `AGENT_STYLE=ASHARIF-STYLE` (the name as the owner writes it, uppercase): fails with the valid-values list, in all three loaders. Strict on purpose, consistent with other keys.
2. `AGENT_STYLE=` (empty) and `AGENT_STYLE_LEARNING=ON`: fail, not silently default.
3. `.env` without either key: loads, defaults `standard` / `on` exported.
4. A style file or `personal.md` missing at run time: `AGENTS.md` says fall back to `standard` and state it.
5. An upgrade must never overwrite a filled-in `personal.md` (ownership tests plus the dry run in Task 6).

---

## File map

| File | Change | Task |
|---|---|---|
| `scripts/local_config.py` | allowed keys, `AGENT_STYLES`, validation | 1 |
| `scripts/load_env.sh` | unset, allowed case, defaults, export, validation | 1 |
| `scripts/load_env.ps1` | remove list, allowed list, defaults, validation | 1 |
| `.env.example` | document both keys | 1 |
| `tests/test_local_config.py`, `tests/test_windows_support.py` | loader tests | 1 |
| `.agents/styles/{README,standard,concise,teaching,asharif-style}.md` | new, template-owned | 2 |
| `.agents/styles/personal.md` | new, project-owned placeholder | 2 |
| `template-manifest.json` | ownership | 2 |
| `tests/test_agent_styles.py` | new contract tests | 2, 3 |
| `AGENTS.md`, `README.md` | conversation-style section, one README paragraph | 3 |
| `docs/superpowers/{specs,plans}/` | copy spec and plan if that folder exists in the template | 4 |

---

### Task 1: Configuration keys in all three loaders

**Files:**
- Modify: `scripts/local_config.py` (lines ~24-31, ~203-206)
- Modify: `scripts/load_env.sh` (lines ~9, ~115, ~160-161, ~419-422)
- Modify: `scripts/load_env.ps1` (lines ~19, ~55, ~99, ~264-266)
- Modify: `.env.example` (after the `UC_APX_SKILLS_AGENT` line)
- Test: `tests/test_local_config.py`, `tests/test_windows_support.py`

**Interfaces:**
- Produces: env vars `AGENT_STYLE` (default `standard`) and `AGENT_STYLE_LEARNING` (default `on`) exported by both shell loaders; `scripts.local_config.AGENT_STYLES` tuple.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_local_config.py`, after `test_legacy_uc_apx_settings_remain_supported_for_this_release_line`:

```python
    def test_agent_style_settings_accept_valid_values(self) -> None:
        for style in ("standard", "concise", "teaching", "asharif-style"):
            for learning in ("on", "off"):
                with self.subTest(style=style, learning=learning):
                    content = VALID_BASE_CONFIG + f"AGENT_STYLE={style}\nAGENT_STYLE_LEARNING={learning}\n"
                    values = read_project_env(self.write_env(content))
                    self.assertEqual(values["AGENT_STYLE"], style)
                    self.assertEqual(values["AGENT_STYLE_LEARNING"], learning)
        # Both keys are optional.
        read_project_env(self.write_env(VALID_BASE_CONFIG))

    def test_agent_style_settings_reject_invalid_values_without_echoing_them(self) -> None:
        style_message = "AGENT_STYLE must be one of: standard, concise, teaching, asharif-style"
        learning_message = "AGENT_STYLE_LEARNING must be on or off"
        for key, value, message in (
            ("AGENT_STYLE", "ASHARIF-STYLE", style_message),
            ("AGENT_STYLE", "Standard", style_message),
            ("AGENT_STYLE", "", style_message),
            ("AGENT_STYLE", "verbose", style_message),
            ("AGENT_STYLE_LEARNING", "ON", learning_message),
            ("AGENT_STYLE_LEARNING", "yes", learning_message),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigError) as ctx:
                    read_project_env(self.write_env(VALID_BASE_CONFIG + f"{key}={value}\n"))
                self.assertEqual(str(ctx.exception), message)
```

Add to `tests/test_windows_support.py` directly after `test_load_env_accepts_preflight_inventory_retry_setting` (it uses the same `self.environment(...)` helper):

```python
    def test_load_env_defaults_and_validates_agent_style(self) -> None:
        base = (ROOT / ".env.example").read_text(encoding="utf-8")
        without = base.replace("AGENT_STYLE=standard\n", "").replace("AGENT_STYLE_LEARNING=on\n", "")
        cases = (
            (without, 0, "standard on", ""),
            (base.replace("AGENT_STYLE=standard\n", "AGENT_STYLE=asharif-style\n")
                 .replace("AGENT_STYLE_LEARNING=on\n", "AGENT_STYLE_LEARNING=off\n"), 0, "asharif-style off", ""),
            (base.replace("AGENT_STYLE=standard\n", "AGENT_STYLE=ASHARIF-STYLE\n"), 1, "",
             "AGENT_STYLE must be one of: standard, concise, teaching, asharif-style"),
            (base.replace("AGENT_STYLE_LEARNING=on\n", "AGENT_STYLE_LEARNING=ON\n"), 1, "",
             "AGENT_STYLE_LEARNING must be on or off"),
        )
        for content, code, expected_out, expected_err in cases:
            with self.subTest(expected_out=expected_out, expected_err=expected_err):
                with tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    environment = self.environment(directory, "Linux")
                    env_file = directory / ".env"
                    env_file.write_text(content, encoding="utf-8", newline="\n")
                    result = subprocess.run(
                        ["bash", "-c", 'source "$1" "$2" && printf "%s %s\\n" "$AGENT_STYLE" "$AGENT_STYLE_LEARNING"',
                         "bash", str(ROOT / "scripts" / "load_env.sh"), str(env_file)],
                        capture_output=True, text=True, env=environment, check=False,
                    )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout.strip(), expected_out)
                self.assertIn(expected_err, result.stderr)

    def test_powershell_env_loader_knows_the_agent_style_settings(self) -> None:
        text = (ROOT / "scripts" / "load_env.ps1").read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count("AGENT_STYLE_LEARNING"), 4)
        self.assertGreaterEqual(text.count("AGENT_STYLE"), 8)
        self.assertIn("AGENT_STYLE must be one of: standard, concise, teaching, asharif-style", text)
        self.assertIn("AGENT_STYLE_LEARNING must be on or off", text)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_local_config tests.test_windows_support -q`
Expected: FAIL (`unknown setting AGENT_STYLE`, missing text in `load_env.ps1`).

- [ ] **Step 3: Implement**

`scripts/local_config.py`: in `ALLOWED_KEYS` add `"AGENT_STYLE",` and `"AGENT_STYLE_LEARNING",` after `"UC_APX_SKILLS_AGENT",`. Above `ALLOWED_KEYS` add:

```python
AGENT_STYLES = ("standard", "concise", "teaching", "asharif-style")
```

After the `UC_APX_SKILLS_AGENT` check (line ~206) add:

```python
    if values.get("AGENT_STYLE", "standard") not in AGENT_STYLES:
        raise ConfigError("AGENT_STYLE must be one of: " + ", ".join(AGENT_STYLES))
    if values.get("AGENT_STYLE_LEARNING", "on") not in ("on", "off"):
        raise ConfigError("AGENT_STYLE_LEARNING must be on or off")
```

`scripts/load_env.sh`:

```bash
# line 9
unset INSTALL_UC_APX UC_APX_SKILLS_AGENT AGENT_STYLE AGENT_STYLE_LEARNING
# allowed-key case (line ~115): replace "INSTALL_UC_APX|UC_APX_SKILLS_AGENT|\" with
    INSTALL_UC_APX|UC_APX_SKILLS_AGENT|AGENT_STYLE|AGENT_STYLE_LEARNING|\
# defaults (after line 160)
if [ "${AGENT_STYLE+x}" != x ]; then AGENT_STYLE=standard; fi
if [ "${AGENT_STYLE_LEARNING+x}" != x ]; then AGENT_STYLE_LEARNING=on; fi
export AGENT_STYLE AGENT_STYLE_LEARNING
# validation (after the UC_APX_SKILLS_AGENT case, line ~422)
case "$AGENT_STYLE" in
  standard|concise|teaching|asharif-style) ;;
  *) project_env_fail "AGENT_STYLE must be one of: standard, concise, teaching, asharif-style"; return 1 2>/dev/null || exit 1 ;;
esac
case "$AGENT_STYLE_LEARNING" in
  on|off) ;;
  *) project_env_fail "AGENT_STYLE_LEARNING must be on or off"; return 1 2>/dev/null || exit 1 ;;
esac
```

`scripts/load_env.ps1`:

```powershell
# line 19: add to the Remove-Item list
  Env:INSTALL_UC_APX, Env:UC_APX_SKILLS_AGENT, Env:AGENT_STYLE, Env:AGENT_STYLE_LEARNING, Env:APEX_WORKSPACE_USERNAME -ErrorAction SilentlyContinue
# line 55: allowed list
  "INSTALL_UC_APX", "UC_APX_SKILLS_AGENT", "AGENT_STYLE", "AGENT_STYLE_LEARNING",
# after line 99: defaults
if (-not $projectEnvSeen.ContainsKey("AGENT_STYLE")) { $env:AGENT_STYLE = "standard" }
if (-not $projectEnvSeen.ContainsKey("AGENT_STYLE_LEARNING")) { $env:AGENT_STYLE_LEARNING = "on" }
# after the UC_APX_SKILLS_AGENT check (line ~266): validation
if ($env:AGENT_STYLE -cnotin @("standard", "concise", "teaching", "asharif-style")) {
  throw "project environment error: AGENT_STYLE must be one of: standard, concise, teaching, asharif-style"
}
if ($env:AGENT_STYLE_LEARNING -cnotin @("on", "off")) {
  throw "project environment error: AGENT_STYLE_LEARNING must be on or off"
}
```

`.env.example`, after `UC_APX_SKILLS_AGENT=universal`:

```
# Purpose: How coding agents talk to you and present decisions. One of
# standard (default), concise, teaching, asharif-style. See .agents/styles/README.md.
AGENT_STYLE=standard

# Purpose: Let agents record your working preferences in .agents/styles/personal.md.
# on (default) or off. Agents still read the file when this is off.
AGENT_STYLE_LEARNING=on
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_local_config tests.test_windows_support -q && bash -n scripts/load_env.sh && ruff check scripts tests`
Expected: OK. If the shell failure test reports the message on stdout instead of stderr, change only `result.stderr` to the stream `project_env_fail` uses. `pwsh` may be missing locally; the PowerShell loader is checked by the text test here and by the `windows-offline` CI job.

- [ ] **Step 5: Commit**

```bash
git add scripts/local_config.py scripts/load_env.sh scripts/load_env.ps1 .env.example tests/test_local_config.py tests/test_windows_support.py
git commit -m "feat: add AGENT_STYLE and AGENT_STYLE_LEARNING settings to the env loaders"
```

---

### Task 2: Style files, learning-layer placeholder, manifest

**Files:**
- Create: `.agents/styles/README.md`, `standard.md`, `concise.md`, `teaching.md`, `asharif-style.md`, `personal.md`
- Modify: `template-manifest.json`
- Create: `tests/test_agent_styles.py`

**Interfaces:**
- Consumes: `scripts.local_config.AGENT_STYLES` (Task 1).
- Produces: style files addressed as `.agents/styles/<AGENT_STYLE>.md`; `personal.md` section headings `## Preferences`, `## Decision habits`, `## Corrections`, `## Vocabulary`; the override sentence `This style changes communication only; the rules in `AGENTS.md` and `.agents/rules/agent-safety.md` always win.`

- [ ] **Step 1: Write the failing tests** — create `tests/test_agent_styles.py`:

```python
"""Contract tests for the agent conversation styles in .agents/styles/."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from scripts.local_config import AGENT_STYLES

ROOT = Path(__file__).resolve().parents[1]
STYLES = ROOT / ".agents" / "styles"
SECTIONS = (
    "## 1. Interaction protocol",
    "## 2. Decision format",
    "## 3. Answer and status format",
    "## 4. Delegation and review",
    "## 5. Evidence and honesty",
    "## 6. Scope control",
    "## 7. Language and input tolerance",
    "## 8. Never overrides",
)
NEVER_OVERRIDES = (
    "This style changes communication only; the rules in `AGENTS.md` and "
    "`.agents/rules/agent-safety.md` always win."
)
PERSONAL_SECTIONS = ("## Preferences", "## Decision habits", "## Corrections", "## Vocabulary")
PRIVATE = (
    re.compile(r"https?://"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    re.compile(r"RUNERP|ABSH|NATREC|DEV26|DEV2\b", re.IGNORECASE),
)
STYLE_LINE_BUDGET = 70
PERSONAL_LINE_BUDGET = 40


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class AgentStyleFileTests(unittest.TestCase):
    def test_every_allowed_style_has_a_file_with_all_sections_and_the_override_sentence(self) -> None:
        for name in AGENT_STYLES:
            with self.subTest(style=name):
                text = read(STYLES / f"{name}.md")
                for heading in SECTIONS:
                    self.assertIn(heading, text)
                self.assertIn(NEVER_OVERRIDES, text)
                self.assertLessEqual(len(text.splitlines()), STYLE_LINE_BUDGET)

    def test_style_files_contain_no_private_details(self) -> None:
        for path in sorted(STYLES.glob("*.md")):
            text = read(path)
            for pattern in PRIVATE:
                with self.subTest(file=path.name, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))

    def test_asharif_style_carries_the_short_example(self) -> None:
        text = read(STYLES / "asharif-style.md")
        self.assertIn("Short example", text)
        for marker in ("**A.", "**B.", "**C.", "(recommended)"):
            self.assertIn(marker, text)

    def test_personal_layer_placeholder_has_the_sections_and_no_entries(self) -> None:
        text = read(STYLES / "personal.md")
        for heading in PERSONAL_SECTIONS:
            self.assertIn(heading, text)
        self.assertLessEqual(len(text.splitlines()), PERSONAL_LINE_BUDGET)
        outside_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        self.assertEqual([l for l in outside_comments.splitlines() if l.startswith("- ")], [])

    def test_styles_readme_lists_every_style_and_the_precedence(self) -> None:
        text = read(STYLES / "README.md")
        for name in AGENT_STYLES:
            self.assertIn(f"`{name}`", text)
        self.assertIn("personal.md", text)
        self.assertIn("AGENT_STYLE_LEARNING", text)


class AgentStyleOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(read(ROOT / "template-manifest.json"))

    def test_style_files_are_template_owned_and_personal_layer_is_project_owned(self) -> None:
        for name in (*AGENT_STYLES, "README"):
            self.assertIn(f".agents/styles/{name}.md", self.manifest["templateOwned"])
        self.assertIn(".agents/styles/personal.md", self.manifest["projectOwned"])
        self.assertNotIn(".agents/styles/personal.md", self.manifest["templateOwned"])
        self.assertFalse([p for p in self.manifest["templateOwned"] if p == ".agents/styles/**"])


class EnvExampleTests(unittest.TestCase):
    def test_env_example_documents_both_keys_with_defaults(self) -> None:
        lines = read(ROOT / ".env.example").splitlines()
        self.assertIn("AGENT_STYLE=standard", lines)
        self.assertIn("AGENT_STYLE_LEARNING=on", lines)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_agent_styles -q`
Expected: FAIL (`FileNotFoundError: .agents/styles/standard.md`).

- [ ] **Step 3: Create the files**

`.agents/styles/standard.md`:

````markdown
# Style: standard

The default. Neutral, complete prose. Nothing here relaxes a rule in `AGENTS.md`.

## 1. Interaction protocol
- Act on clear requests. Ask only when you are blocked or the answer would change the action.
- Ask one question at a time and say what you will do if no answer comes.

## 2. Decision format
- Offer two or three options with a one-line trade-off each, then recommend one and say why.
- Do not start a design that needs approval until the developer agrees.

## 3. Answer and status format
- Lead with the outcome, then the details that matter.
- Status: what is done, what is in progress, what is blocked, what you need from the developer.

## 4. Delegation and review
- Review work done by sub-agents before reporting it complete, and say what you checked.

## 5. Evidence and honesty
- Say which checks ran and which did not. Never report an unrun check as passed.
- Label observations from shared development data as observations, not as intent.

## 6. Scope control
- Do what was asked. Mention nearby problems; do not fix them unasked.

## 7. Language and input tolerance
- Reply in the language the developer writes in. Read typos and shorthand for intent.

## 8. Never overrides
This style changes communication only; the rules in `AGENTS.md` and `.agents/rules/agent-safety.md` always win.
````

`.agents/styles/concise.md`:

````markdown
# Style: concise

Minimal output. Results, blockers and decisions only.

## 1. Interaction protocol
- Act on clear requests. Ask only when blocked, in one line.
- Do not narrate steps; report the result.

## 2. Decision format
- Give the recommended choice first and the alternatives in one line each.
- Keep the question to one sentence.

## 3. Answer and status format
- One to five lines. Result first. No background unless asked.
- Use a table only to compare three or more items.
- Status in four short lines: done, running, waiting on you, decisions.

## 4. Delegation and review
- Report only what the review found, not that it happened.

## 5. Evidence and honesty
- Name checks that did not run. Show a command or diff only when it changes the decision.

## 6. Scope control
- Do exactly what was asked. List nearby problems in one line, do not fix them.

## 7. Language and input tolerance
- Reply in the developer's language. Read typos and shorthand for intent.

## 8. Never overrides
This style changes communication only; the rules in `AGENTS.md` and `.agents/rules/agent-safety.md` always win.
````

`.agents/styles/teaching.md`:

````markdown
# Style: teaching

Explains the why. Longer answers are expected.

## 1. Interaction protocol
- Before an action, say in plain words what it does and why it is needed.
- Define a term the first time you use it. Ask one question at a time.

## 2. Decision format
- Explain the problem and its cause first, then two or three options.
- For each option give the effect, a concrete example and the cost of choosing it. Recommend one.

## 3. Answer and status format
- Structure: what happened, why, what to check next.
- Explain Oracle, APEX or tool behaviour that the developer may not know when it affects the result.

## 4. Delegation and review
- Say what each sub-agent was asked to do and what you verified in its result.

## 5. Evidence and honesty
- Show the code or command and explain each important part of it.
- Say which checks ran and which did not. Never report an unrun check as passed.

## 6. Scope control
- Do what was asked. Explain nearby problems, and let the developer decide whether to fix them.

## 7. Language and input tolerance
- Reply in the developer's language. Read typos and shorthand for intent, and restate what you understood.

## 8. Never overrides
This style changes communication only; the rules in `AGENTS.md` and `.agents/rules/agent-safety.md` always win.
````

`.agents/styles/asharif-style.md`:

````markdown
# Style: asharif-style

Distilled rules from a developer who decides quickly by lettered options and wants evidence.

## 1. Interaction protocol
- One decision at a time. Explain the problem and its cause in plain words before the options.
- Accept "option a", "a", "yes" as complete answers. Number every list of pending items,
  findings or questions so replies can be "1- yes 2- later 3- ignore".
- When asked "status", "pending" or "remaining", answer in this shape: done, running, waiting on you,
  decisions needed. After a merge or finished task, list the remaining tasks without being asked.

## 2. Decision format
- Lettered options. Put the recommended one first and mark it "(recommended)".
- Every option set carries a short example by default: one concrete case worked through each option.
- If asked for "an example and ask again", give a different case and repeat the question.

## 3. Answer and status format
- Lead with the answer. Tables only for comparisons.
- Show code as the changed routines only, not whole before and after files.

## 4. Delegation and review
- When sub-agents do the work, review their result and report findings, not just completion.

## 5. Evidence and honesty
- List the exact commands run and their timings when work was executed.
- Label development data as observation (it may be test data); base conclusions on code and stated rules.
- Never say a browser or UI check happened unless it did.
- Tag each new or changed code block with a short comment: `DEVELOPER_NAME` from `.env`, the date, and one line on what the block does.

## 6. Scope control
- Plan-only means plan only. Wait for approval before implementing a reviewed design.
- "leave that", "ignore" and "change nothing" end a topic; do not expand it.
- Park postponed items in the project's to-do file and say where.

## 7. Language and input tolerance
- Messages may be terse, in capitals, or contain typos or non-English words. Infer the intent and ask
  only when the meaning would change the action.

## 8. Never overrides
This style changes communication only; the rules in `AGENTS.md` and `.agents/rules/agent-safety.md` always win.

### Short example
> The 24-hour cancel rule is enforced for customers only; staff can cancel at any time.
> **A. Keep it (recommended).** A supervisor cancels tomorrow's 08:00 visit at 20:00 today: allowed and logged.
> **B. Apply the 24 h rule to staff.** Same case: blocked until an admin overrides.
> **C. Remove the rule.** A customer could cancel one hour before. Which one?
````

`.agents/styles/personal.md`:

````markdown
# Personal style layer

<!-- Project-owned: template upgrades never overwrite this file. Applied over AGENT_STYLE.
Agents add one dated line per learned preference, e.g.
"- 2026-10-15: numbered lists so replies can be 1- yes 2- later (asked twice)".
Record: an explicit preference or correction, or the same choice three times. Say so in the reply.
Replace contradicting entries. Keep this file under 40 lines. Never record credentials, hosts,
project, schema or business data, or anything that relaxes a safety rule. Edit freely. -->

## Preferences

## Decision habits

## Corrections

## Vocabulary
````

`.agents/styles/README.md`:

````markdown
# Conversation styles

`AGENT_STYLE` in the root `.env` selects how coding agents talk to you and present decisions.

| Value | Use |
|---|---|
| `standard` (default) | Neutral, complete prose |
| `concise` | Results, blockers and decisions only |
| `teaching` | Explains the reasoning and the background |
| `asharif-style` | Lettered options with examples, numbered lists, fixed status shape, evidence first |

Agents read the style at the start of every conversation and then `.agents/styles/personal.md`,
which is yours and is applied over the style. Precedence: `AGENTS.md`, `.agents/rules/agent-safety.md`
and project rules, then `personal.md`, then the style. A style never relaxes a safety rule.

`AGENT_STYLE_LEARNING=on` (default) lets agents record your working preferences in `personal.md`,
one dated line each, announced in the reply. Set `off` to stop writing; agents still read the file.
Edit or delete any entry at any time. The file is yours to commit or not.

To add a style, add `.agents/styles/<name>.md` with the same eight sections and add the name to the
three env loaders and `tests/test_agent_styles.py`.
````

`template-manifest.json`: in `templateOwned`, after `".agents/rules/graphify.md",` insert:

```json
    ".agents/styles/README.md",
    ".agents/styles/standard.md",
    ".agents/styles/concise.md",
    ".agents/styles/teaching.md",
    ".agents/styles/asharif-style.md",
```

and in `projectOwned`, after `".agents/rules/project.md",` insert `".agents/styles/personal.md",`.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_agent_styles tests.test_template_manifest tests.test_upgrade_template -q`
Expected: OK. If a manifest test reports an unowned `.agents/styles/...` path, add that exact path; never switch to a glob.

- [ ] **Step 5: Commit**

```bash
git add .agents/styles template-manifest.json tests/test_agent_styles.py
git commit -m "feat: add the four conversation styles and the personal learning layer placeholder"
```

---

### Task 3: Agent instruction and README

**Files:**
- Modify: `AGENTS.md` (new section before `## Configuration and application source`)
- Modify: `README.md` (one short paragraph)
- Test: `tests/test_agent_styles.py`

**Interfaces:**
- Consumes: style file names (Task 2), key names (Task 1).

- [ ] **Step 1: Write the failing test** — append to `tests/test_agent_styles.py`, before the `if __name__` line:

```python
class AgentInstructionTests(unittest.TestCase):
    def test_agents_md_enforces_the_style_at_conversation_start(self) -> None:
        text = read(ROOT / "AGENTS.md")
        self.assertIn("## Conversation style", text)
        position = text.index("## Conversation style")
        self.assertLess(position, text.index("## Configuration and application source"))
        for needle in (
            "start of every conversation",
            "AGENT_STYLE",
            "AGENT_STYLE_LEARNING",
            ".agents/styles/",
            ".agents/styles/personal.md",
            "Style: ",
            "fall back to `standard`",
            "Recorded in personal.md",
            "never relaxes",
            "never commit",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text.lower() if needle.islower() else text)

    def test_readme_points_to_the_styles_guide(self) -> None:
        self.assertIn(".agents/styles/README.md", read(ROOT / "README.md"))
```

(`needle.islower()` handles the two lower-case needles that appear mid-sentence; if a phrase is capitalised in the text, adjust the needle, not the test logic.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_agent_styles.AgentInstructionTests -q`
Expected: FAIL (`## Conversation style` not found).

- [ ] **Step 3: Implement** — insert in `AGENTS.md` immediately before `## Configuration and application source`:

````markdown
## Conversation style

At the start of every conversation, before your first reply:

1. Read `AGENT_STYLE` (default `standard`; one of `standard`, `concise`, `teaching`,
   `asharif-style`) and `AGENT_STYLE_LEARNING` (default `on`) from the root `.env`.
2. Read `.agents/styles/<AGENT_STYLE>.md` and follow it for the whole conversation.
3. Read `.agents/styles/personal.md` if it exists and apply it over the style.
4. Open your first reply with one line: `Style: <name>`, plus ` + personal layer (N entries)`
   when `personal.md` has entries. If the value is invalid or a file is missing, fall back to
   `standard` and say so in that line.

Precedence, highest first: this file, `.agents/rules/agent-safety.md` and the project rules; then
`personal.md`; then the style. A style or a personal entry never relaxes a gate (commits and
pushes, database writes, migrations, imports, claiming a check that did not run).

Learning, unless `AGENT_STYLE_LEARNING=off`: when the developer states a preference or corrects your
format, or makes the same choice three times, add one dated line to `personal.md` under the matching
heading and say `Recorded in personal.md: <entry>` in the same reply. Never record silently. Replace
an entry that the new one contradicts. Keep the file under about 40 lines. Never record credentials,
hosts, customer, employee or business data, project or schema names, or anything that loosens a
safety rule. Never commit or push `personal.md` on your own initiative. See
[.agents/styles/README.md](.agents/styles/README.md).

````

`README.md`: add after the paragraph that mentions `AGENTS.md` and `CLAUDE.md` (around line 55):

```markdown
Agents also load a conversation style at the start of each conversation: set `AGENT_STYLE` in
`.env` (`standard`, `concise`, `teaching` or `asharif-style`). Your own preferences accumulate in
`.agents/styles/personal.md`; see [.agents/styles/README.md](.agents/styles/README.md).
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_agent_styles tests.test_documentation_contract -q`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md README.md tests/test_agent_styles.py
git commit -m "feat: load the conversation style at the start of every agent conversation"
```

---

### Task 4: Verify and open the 26.1 PR

**Files:** none new; optional copy of the spec and plan.

- [ ] **Step 1: Branch and bring the work in.** In the 26.1 clone (`scratchpad/tpl26`, remote `ash2osh/APEX_PROJECT_TEMPLATE_26_1`): `git checkout main && git pull --ff-only && git checkout -b feat/agent-conversation-style`, then do Tasks 1-3 there (commit after each).

- [ ] **Step 2: Copy the design documents if the template carries them.** If `docs/superpowers/specs/` exists in the clone, copy the spec and this plan into `docs/superpowers/specs/` and `docs/superpowers/plans/` (they are `templateOnly`), commit `docs: add the conversation style spec and plan`. If the folder is absent, skip.

- [ ] **Step 3: Full local check, in parallel with the push**

Run: `python3 -m compileall -q scripts tools tests && ruff check scripts tools tests && for s in scripts/*.sh; do bash -n "$s"; done && python3 -m unittest discover -s tests -q`
Expected: OK (a few tests may skip). Known flake: the 1.5 s interrupt timing test fails under load; rerun it alone before treating it as a failure.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feat/agent-conversation-style
gh pr create --repo ash2osh/APEX_PROJECT_TEMPLATE_26_1 --base main --head feat/agent-conversation-style \
  --title "feat: agent conversation styles and personal learning layer" \
  --body "Adds AGENT_STYLE (standard, concise, teaching, asharif-style) and AGENT_STYLE_LEARNING to .env, four template-owned style files, a project-owned .agents/styles/personal.md, and the AGENTS.md start-of-conversation instruction. Spec: docs/superpowers/specs/2026-10-15-agent-conversation-style-design.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 5: CI green, then merge.** `gh pr checks <number> --repo ash2osh/APEX_PROJECT_TEMPLATE_26_1 --watch`. Expected: `offline`, `windows-offline`, `macos-installer` pass. Fix failures on the branch (Ruff B023 and the `windows-offline` PowerShell text test are the likely ones). Merge with a merge commit: `gh pr merge <number> --repo ash2osh/APEX_PROJECT_TEMPLATE_26_1 --merge`. Confirm `main` CI is green after the merge.

---

### Task 5: Port to 26.2

**Files:** same as Tasks 1-3 in `scratchpad/tpl262`.

- [ ] **Step 1: Produce and apply the patch**

```bash
cd scratchpad/tpl26 && git format-patch origin/main~N..origin/main -o ../style-patches   # N = number of commits of this feature on main
cd ../tpl262 && git checkout main && git pull --ff-only && git checkout -b feat/agent-conversation-style
git am -3 ../style-patches/*.patch
```

If `git am` stops on a conflict, resolve it by keeping both sides' surrounding lines, `git add`, `git am --continue`; never leave a `.orig` or `.rej` file (`git status` must be clean of them).

- [ ] **Step 2: Run the same checks as Task 4 Step 3.** Expected: OK. 26.2 differs from 26.1 in `compare_schema.py` and migration code only; none of the files here, so failures mean a conflict resolution error.

- [ ] **Step 3: Push, open the PR against `ash2osh/APEX_PROJECT_TEMPLATE_TEAM`, wait for CI, merge with a merge commit**, using the commands of Task 4 Steps 4-5 with that repository name.

---

### Task 6: Pull into natrec_team and activate

**Files:** `natrec_team` working tree (no commit unless the owner asks).

- [ ] **Step 1: Dry run.** `scripts/team.sh upgrade-template --dry-run`. Expected: lists `.agents/styles/*.md` (five template-owned), `AGENTS.md`, `README.md`, `.env.example`, the three loaders and the manifest; does not list `.agents/styles/personal.md` as overwritten. Check the working tree first: the 59 uncommitted App 100/115/120 export files are unrelated, leave them.
- [ ] **Step 2: Apply.** `scripts/team.sh upgrade-template`. Review `git status --short` and the `AGENTS.md` diff. Resolve the ownership surprise if `AGENTS.project.md` text conflicts with the new section (ask the owner which applies).
- [ ] **Step 3: Activate.** Add `AGENT_STYLE=asharif-style` and `AGENT_STYLE_LEARNING=on` to the root `.env` (comment-free lines, no credentials).
- [ ] **Step 4: Verify.** `source scripts/load_env.sh .env && echo "$AGENT_STYLE $AGENT_STYLE_LEARNING"` prints `asharif-style on`; `python3 -m unittest tests.test_natrec_api_cli -q` still passes; the next conversation's first reply starts with `Style: asharif-style`.
- [ ] **Step 5: Report** with the commands run and their timings. Commit and push in `natrec_team` only if the owner asks.

---

## Time estimate (about 5 hours, down from 14-16)

| Task | Hours |
|---|---|
| 1 Loaders and tests | 1.0 |
| 2 Style files, manifest, tests | 1.25 |
| 3 AGENTS.md, README, tests | 0.5 |
| 4 Verify, 26.1 PR, CI wait | 0.75 |
| 5 Port to 26.2, PR, CI wait | 0.75 |
| 6 Natrec upgrade and activation | 0.5 |
| Final whole-branch review | 0.25 |

What shortened it: the `style` command and `doctor` line were dropped; one new test file; 26.2 is a patch, not a rewrite; the full suite runs once per repo while CI runs in parallel; one review of the whole branch instead of one per task.

## Self-review

- Spec coverage: config keys (Task 1), four styles with eight sections and the ASHARIF short example (Task 2), learning layer file, sections, upgrade ownership (Tasks 2-3), start-of-conversation enforcement with the first-reply line and fallback (Task 3), both template lines (Tasks 4-5), downstream activation (Task 6), privacy scan and tests (Task 2). The dropped `style` command and `doctor` line are recorded in the spec as out of scope.
- Placeholders: none; every code step carries its code. The only run-time unknowns are the two named adjustments (output stream of `project_env_fail`; `N` in `format-patch`).
- Names consistent: `AGENT_STYLES`, `AGENT_STYLE`, `AGENT_STYLE_LEARNING`, the override sentence and the heading lists are identical in the tests, files and `AGENTS.md`.
