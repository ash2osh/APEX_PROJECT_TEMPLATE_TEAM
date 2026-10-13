# Reporting template feedback upstream

Use this guide when you confirm a defect in inherited template scripts,
documentation, agent rules, or tooling. Keep ordinary application and project
issues in the local [TODO.md](../TODO.md). Check
[known limitations](known-limitations.md) first; documented behavior is
intentional unless evidence shows it crossed its stated boundary. If the source
of an issue is uncertain, record that uncertainty instead of calling it an
upstream defect.

## Record and route the finding

1. Give each inherited-template finding a stable local feedback ID and name its
   component. Search `TODO.md` by ID and component/finding before adding it. If
   the finding already has a record, update that record, keep its ID, and
   append concise dated status/evidence history; do not create a duplicate or
   erase prior history. For an older record without an ID, add the ID and
   component to that same entry.
2. Add a sanitized entry to the project-owned `TODO.md`. Include the template
   release/ref/commit, a short reproduction, what happened, what was expected,
   local fix or workaround status, and whether upstream reporting is queued,
   linked, or reported.
3. Resolve the template repository from `.template-lock.json`'s `upstream`;
   only when that value is absent, use `template-manifest.json`'s `upstream`.
   Confirm it is the intended GitHub template repository before querying or
   reporting. If selected provenance is missing, malformed, ambiguous,
   non-GitHub, or embeds credentials, do not query or report through it. Keep
   the URL redacted in `TODO.md`, record why routing is unsafe, and leave the
   finding queued. Never substitute the downstream project's `origin`, transfer
   downstream commits, or guess the repository.
4. Search the verified upstream's existing issues and pull requests. Link a
   matching report in `TODO.md`; add only materially new, sanitized evidence
   to it. If no match exists, automatically open a concise upstream issue by
   default and add its returned URL to `TODO.md`.
5. Use a focused, tested pull request when the current task also authorizes the
   required upstream-only commit and push. The change must contain only work
   for the verified template repository. Do not commit or push downstream work
   as part of reporting.

Do not include passwords, tokens, credential-bearing URLs, connection details,
customer or project source/data, private salts, full logs, or unreviewed
payloads. For a sensitive vulnerability, use a private maintainer security
route only when one is available. If no private route is available, keep only a
redacted local record with the reason and `queued` status; do not publish the
details through a public issue. If authentication, network access, or safe
disclosure is unclear, keep a redacted local record with the reason and
`queued` status, then continue the safe local task. Do not wait for an upstream
response or claim a report exists without its URL. Issue creation records a
URL; it does not edit an upstream `TODO.md`. Never auto-merge, deploy, or
upgrade from a report.

## TODO record shape

Keep one small entry per issue. Update it as local and upstream state changes:

```markdown
- [ ] **Short title**
  - Feedback ID: TF-001
  - Component: `scripts/<affected-file>`
  - Kind: inherited-template issue
  - Local fix/workaround: in progress
  - Upstream report: reported
  - Upstream URL: https://github.com/owner/repository/issues/123
  - Template release/ref/commit: 26.2 / main / <observed commit>
  - Sanitized reproduction/evidence: <steps and ignored local evidence path>
  - History: <append dated status/evidence changes; retain prior entries>
```

Replace every example value with observed information and assign a unique,
stable local ID to inherited-template findings. Keep ignored diagnostic
artifacts local and summarize only the evidence needed to reproduce the issue.
For sensitive findings without a private route, omit sensitive details and
artifact paths; retain only a redacted queued summary.
