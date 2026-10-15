# Agent conversation style — design

Date: 2026-10-15. Author tag: ASHARIF. Target: the 26.1 and 26.2 template repositories
(two PRs, 26.1 first), then `upgrade-template` into downstream projects.
Status: draft for owner review. Nothing is implemented.

## Purpose

Let each developer choose how coding agents talk to them and present decisions, with one
`.env` setting that every agent reads at the start of a conversation. A style changes
communication only. It never relaxes a safety rule.

## Decisions already made by the owner

1. Default style is `standard`. The owner opts into `asharif-style` in their own `.env`.
2. Four styles: `standard` (default), `concise`, `teaching`, `asharif-style`.
3. The public template contains only distilled rules for `asharif-style`: no quotes, names,
   hosts, project identifiers or data from private conversations.
4. Enforced at the start of every conversation through the agent instruction file, for any
   coding agent (not a Claude-only hook).
5. Both template lines (26.1 and 26.2).
6. `asharif-style` includes a short example with every decision it presents.
7. An extra file learns from the developer's behaviour and keeps it as a customization layer
   over whichever style is chosen (section "Personal learning layer").

## Configuration

`.env` key `AGENT_STYLE`, optional, default `standard`. Allowed values (lowercase, exact):
`standard`, `concise`, `teaching`, `asharif-style`. Any other value is a configuration
error with the list of valid values (same pattern as `UC_APX_SKILLS_AGENT`).

Second key `AGENT_STYLE_LEARNING`, optional, `on` or `off`, default `on`. `off` stops agents
from writing to the personal layer (they still read it).

Touched files: `.env.example` (documented keys), `scripts/local_config.py` (allowed key and
validation), `scripts/load_env.sh` and `scripts/load_env.ps1` (parse, default, validate,
export; behaviour must match between the two). The `doctor` display is dropped from this
change: the agent's own first-reply line already shows the active style.

## Style files (template-owned)

`.agents/styles/README.md` (how styles work, how to add one), `standard.md`, `concise.md`,
`teaching.md`, `asharif-style.md`. Every style file has the same sections so they can be
tested mechanically:

1. Interaction protocol (how to ask, how many questions at a time)
2. Decision format
3. Answer and status format
4. Delegation and review
5. Evidence and honesty
6. Scope control
7. Language and input tolerance
8. Never overrides (a fixed sentence: the safety rules in `AGENTS.md` and
   `.agents/rules/agent-safety.md` always win)

### Standard

Today's behaviour made explicit: complete, neutral prose; ask only when blocked; summarize
what was done and what was verified.

### Concise

Result first, one to five lines. Report only results, blockers and decisions. No background
unless asked. Tables only for comparisons of three or more items.

### Teaching

Explain the reasoning and the Oracle/APEX background behind each step; define terms on first
use; show what each command does before running it; end with what to check next. Longer
answers are expected.

### ASHARIF-STYLE (distilled rules)

Derived from reviewing 218 owner messages in 7 earlier sessions (2026-10-05 to 2026-10-15).
Only the rules are kept.

- **One decision at a time, as lettered options.** Put the recommended option first and mark
  it "(recommended)". State the consequence of each option in one or two lines.
- **Every option set carries a short example by default** (one concrete case per option, for
  instance the same visit handled under A, B and C) so the owner can answer without asking
  "give me an example and ask again".
- **Explain the problem in plain words before the options**: what the issue is and why it
  happens, then the choices.
- **Answer by number.** Lists of pending items, findings and questions are numbered so the
  owner can reply "1- yes 2- later 3- ignore". Accept "option a", "a", "yes" as complete
  answers.
- **Fixed status shape** when asked "status", "pending" or "remaining": done, running,
  waiting on you, decisions needed. After a merge or finished task, list the remaining tasks
  without being asked.
- **Evidence over claims.** Show the code or the changed routines only, not whole before/after
  files. List the exact commands run and their timings when work was executed. Label database
  data as an observation (development data may be test data); base conclusions on code and
  stated rules. Never say a browser or UI check happened unless it did.
- **Delegate, then review.** When work goes to sub-agents, the main agent reviews the result
  and reports findings, not just completion.
- **Gates.** Plan-only means plan only. Wait for approval before implementing a reviewed
  design. Commit and push only when asked. Park postponed items in the project's to-do file
  and say where.
- **Scope control.** "leave that", "ignore" and "change nothing" end the topic; do not expand.
- **Input tolerance.** Messages may be terse, in capitals, or contain typos or Arabic words.
  Infer the intent; ask only when the meaning would change the action. Reply in English.
- **Change comments.** Tag each new or changed code block with a short comment naming
  `DEVELOPER_NAME` from `.env` and the date, and explain the block in one line.

Short example (included in the style file):

> The 24-hour cancel rule is enforced only for customers; staff can cancel at any time.
> **A. Keep it (recommended).** A supervisor cancels a visit tomorrow morning at 20:00 today:
> allowed, and logged. **B. Apply the 24 h rule to staff too.** Same case: blocked, the
> supervisor needs an admin override. **C. Remove the rule.** Customers could cancel an hour
> before. Which one?

## Personal learning layer

A developer-owned file, `.agents/styles/personal.md`, records how this developer likes to
work. It sits on top of any chosen style, so a `standard` or `teaching` user can also have
their own habits applied. Each downstream project is one developer's own repository, so the
file is per developer and is committed like `.env`, only when the developer asks.

**Precedence (highest first):** safety rules and project rules, then `personal.md`, then the
chosen style file. The personal layer can change format, order, length and defaults. It can
never relax a gate (commit/push, database writes, migrations, claiming unrun checks).

**File shape.** The template ships `.agents/styles/personal.md` as a project-owned placeholder
(the manifest test requires every project-owned path to exist in the template). It has no
entries; the agent adds entries to it. Sections:

1. Preferences (how answers, status and decisions should look)
2. Decision habits (for example "usually accepts the recommended option; overrides it for
   business-rule questions")
3. Corrections (things the developer told the agent to stop or start doing)
4. Vocabulary (shorthand the developer uses and what it means)

Each entry is one line with a date and a short reason: `- 2026-10-15: lists of pending items
numbered so replies can be "1- yes 2- later" (asked for twice)`.

**What the agent records, and when.**

- Record when the developer states a preference or corrects the agent's format ("give me an
  example and ask again", "leave that"), or when the same choice pattern appears at least
  three times. A single ambiguous reply is not a pattern.
- Say so in the same reply, in one line: `Recorded in personal.md: <entry>`. Never record
  silently.
- Before adding, check for an existing or contradicting entry; update or replace it rather
  than appending a duplicate. The newest explicit instruction wins.
- Keep the file under about 40 lines; when it grows, consolidate older entries into rules.
- Never record credentials, tokens, host names, customer, employee or business data, project
  or schema names, or anything that would loosen a safety rule. Record rules, not
  transcripts.
- The developer can edit or delete any entry by hand or by saying so ("forget that").
  `AGENT_STYLE_LEARNING=off` stops writing.
- Never commit or push the file on the agent's own initiative.

**Reading.** At conversation start the agent reads the chosen style first, then
`personal.md` if it exists, and applies them in the precedence above. The first-reply line
becomes `Style: asharif-style + personal layer (N entries)`.

**Relation to agent memory.** Some agents keep private memory. The personal layer is the
agent-neutral record in the repository, so every agent and every machine sees the same
preferences. It does not replace project facts, which stay in the project documents.

**Seeding in this project.** `personal.md` starts empty; the `asharif-style` base already
carries the rules distilled from past conversations, and the layer only learns what changes
after that.

## Enforcement at conversation start

`AGENTS.md` gets a "Conversation style" section placed before the other agent rules:

- At the start of every conversation, before the first reply, read `AGENT_STYLE` from the
  root `.env` (default `standard`), then read `.agents/styles/<value>.md` and follow it for
  the whole conversation.
- State the active style in the first reply in one line (`Style: asharif-style`) so the
  developer can see it was applied.
- Then read `.agents/styles/personal.md` if it exists and apply it over the style.
- If the value is invalid or the file is missing, fall back to `standard` and say so.
- During the conversation, record learned preferences into `personal.md` under the rules of
  the personal learning layer, unless `AGENT_STYLE_LEARNING=off`.
- Safety rules and project rules always override a style. A conflict is reported, not
  silently resolved.

`CLAUDE.md` imports `AGENTS.md` already, so Claude Code receives the same instruction. Other
agents that read `AGENTS.md` (Codex, Antigravity and similar) need no extra file. No hook, no
generated file, no change to `.claude/settings.json`.

## Upgrade ownership

`template-manifest.json`: add the five style files (`README.md`, `standard.md`, `concise.md`,
`teaching.md`, `asharif-style.md`) to `templateOwned` by exact path, not by glob, because
`personal.md` lives in the same folder and a path may have only one owner. Downstream
`.template-lock.json` records the new files on the first upgrade. Add `.agents/styles/personal.md` to `projectOwned`, so
an upgrade never overwrites or deletes what was learned. The `AGENT_STYLE` and
`AGENT_STYLE_LEARNING` values live in the developer's own `.env`, which upgrades never touch.

## Out of scope

A `scripts/team.sh style` command and a `doctor` line were considered and dropped to keep the
change small; add them later if the first-reply line proves insufficient.

## Tests

- Config: default is `standard`; each valid value is accepted; an invalid value fails with the
  valid list; behaviour identical in `load_env.sh`, `load_env.ps1` and `local_config.py`.
- Files: every value in the allowed list has a style file; every style file has all eight
  sections; every style file contains the "never overrides" sentence; `asharif-style.md`
  contains the short example.
- Privacy: no style file contains a URL, host name, e-mail address, schema name or project
  name (regex scan).
- Instruction: `AGENTS.md` contains the conversation-style section and names the `.env` key
  and the directory.
- Learning layer: `AGENT_STYLE_LEARNING` accepts `on`/`off`, default `on`, rejects others in
  all three loaders; `personal.md` has the four sections and passes the privacy scan;
  `AGENTS.md` states the precedence and the "never record silently" and "never relax a gate"
  rules; `projectOwned` lists `personal.md`; the manifest tests prove ownership, and the
  upgrade dry run on a real project leaves an edited `personal.md` untouched.
- Manifest: `.agents/styles/**` is template-owned; the manifest test and the upgrade dry run
  pass.
- Docs: `README.md` and `docs/GETTING_STARTED.md` mention the setting.

## Delivery

1. Review this spec.
2. Implementation plan (writing-plans skill).
3. 26.1 branch and PR, CI green (offline, windows-offline, macos-installer), merged.
4. Same change ported to 26.2, same checks.
5. Downstream natrec_team: `upgrade-template`, set `AGENT_STYLE=asharif-style` in `.env`,
   commit only on request.

Estimate: about 5 hours (see the implementation plan for the breakdown).

## Risks and limits

- Agents follow `AGENTS.md` voluntarily. The one-line "Style:" announcement makes a missed
  application visible, but it cannot be enforced technically.
- Style text can grow too long and cost context on every conversation. Target at most about
  60 lines per style file.
- Learning can drift or record a wrong inference. Mitigations: three-occurrence rule for
  patterns, a one-line notice on every write, dated entries with reasons, a size cap, and
  manual edit or deletion at any time.
- An agent that cannot write files (read-only session) cannot learn; it reads the layer only.
- `asharif-style` encodes one person's habits. Other developers choose their own style; a
  project-level custom style is out of scope for this change.
