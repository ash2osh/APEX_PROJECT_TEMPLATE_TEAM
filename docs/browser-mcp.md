# Browser MCP selection

Choose the browser MCP for this checkout (PowerShell users can run scripts/team.ps1):

    scripts/team.sh mcp setup

The command prompts for Chrome, Firefox, both, or none. For scripts and project initialization, pass the choice directly:

    scripts/team.sh mcp setup --choice firefox
    scripts/team.sh mcp status

The selection belongs to the local checkout. Setup writes ignored configuration files: .mcp.json for clients using the MCP JSON format, .codex/config.toml for Codex, and .browser-mcp-selection.json for status. It preserves unrelated MCP entries and refuses to overwrite a conflicting browser entry or tracked client config. All three generated files must be ignored by Git; setup checks this before writing. Move tracked config out of Git first. Setup itself does not start a browser or an MCP process. Re-run setup to change the choice.

Codex reads project settings only after the checkout is trusted. Reload the coding client after changing the selection. The selected MCP package is launched by the client; Node.js and npm/npx must be available then. Setup itself needs Python 3.10 or newer.

## Browser choices

- Chrome starts the pinned Chrome DevTools MCP with auto-connect enabled.
- Firefox attaches to an existing Firefox session with the pinned Firefox DevTools MCP.
- Both enables both configured servers. Choose the browser that matches the task and use one connection for that browser.
- None disables both project-scoped Codex servers and removes the managed browser entries from .mcp.json.

The pinned packages are `chrome-devtools-mcp@1.9.0` and `@mozilla/firefox-devtools-mcp@0.10.6`. See the [Firefox DevTools MCP skill](../.agents/skills/firefox-devtools-mcp/SKILL.md) for attaching and inspecting Firefox.

## Project-owned browser rules

Downstream projects may have browser routing rules in `AGENTS.project.md` or `.agents/rules/project.md`. Template upgrades preserve those files. After changing the browser selection, review any local browser rules and align them with this selection; otherwise they may direct the agent to a different client.

Firefox must be started with remote debugging and Marionette enabled. Use a dedicated Firefox profile: the MCP can access its tabs, cookies, and signed-in sessions. On Linux, for example:

    firefox --marionette --remote-debugging-port

Restart Firefox normally after MCP work. Marionette changes browser fingerprint signals and should not stay enabled for everyday browsing. Do not put login credentials or session URLs in MCP arguments or reports.

## Chrome daemon

The existing Chrome daemon remains available as a separate fallback. When the Chrome MCP is selected, use that direct project MCP and do not start or call the daemon. If Chrome is not selected, use the daemon only when the task asks for Chrome browser work and startup is authorized with user consent. Never attach both routes to the same Chrome session. See the [Chrome daemon guide](CHROME_DEVTOOLS_MCP.md).

Browser actions can affect real applications. Follow the project and APEX safety rules before submitting forms, changing Builder state, or modifying app data.
