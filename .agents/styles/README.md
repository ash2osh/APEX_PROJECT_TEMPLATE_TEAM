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
