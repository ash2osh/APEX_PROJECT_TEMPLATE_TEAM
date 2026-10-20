# Template parity: 26.2 and 26.1

This repository (APEX 26.2) and `APEX_PROJECT_TEMPLATE_26_1` share most files but are
separate repositories, so a fix made in one is easy to miss in the other.
`tools/template_parity.py` lists the shared files whose committed content differs and checks
each against [template-parity.json](template-parity.json):

| Status | Meaning |
| --- | --- |
| `unreviewed` | differs and nobody has looked at it |
| `changed` | reviewed, but either side changed since (recheck it) |
| `missed` | a fix one line has and the other still needs (to port) |
| `feature` | a feature only one line has; porting it is a separate decision |
| `intended` | the release lines differ on purpose |
| `stale` | an entry for files that now match; remove it |

```bash
python3 tools/template_parity.py ../APEX_PROJECT_TEMPLATE_26_1
python3 tools/template_parity.py ../APEX_PROJECT_TEMPLATE_26_1 --mark scripts/x.py --status missed --reason "26.1 has the Windows path fix"
```

Run it after a fix lands in either repository. It exits 1 while anything is `unreviewed` or
`changed`. Both checkouts are read at their committed `HEAD`. The tool and this list are
template-only: projects never receive them.

First pass 2026-10-19: 117 differing files, 14 classified (6 `missed`, 1 `feature`,
7 `intended`), 103 still `unreviewed`.
