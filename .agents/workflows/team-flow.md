# Team workflow

1. Edit `apps/<alias>/` or author a migration pair.
2. For Builder work, run `team.py export-app <alias>`, review the retained
   reconciliation, and commit. Two developers may export concurrently; a
   capture that straddles an import is discarded.
3. Import only as an explicit coordinated reset to a committed source. Post
   the pause notice, verify the baseline/receipt guard, and wait for the
   verified all-clear.
4. For schema work, run drift checks, plan dependencies, apply through the
   metadata mutex, and merge an applied bundle promptly.
5. Promotion uses an exact verified release artifact and disposable replay;
   production receives a human runbook rather than an automated write.
