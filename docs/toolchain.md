# Qualified team toolchain

The team workflow is intentionally conservative about the tools that can touch
an application. A Python core launches SQLcl with an argv list, a regular
empty stdin file, UTF-8 output, and a generated driver that records identity
before and after the payload. Shell and PowerShell files are compatibility
launchers only.

Before a live run, record the outputs of `sql -version`, `java -version`, and
the APEX release shown by the verified identity/doctor query. A live acceptance
run must use explicitly provisioned disposable application IDs; `docker-demo`
is not a default target for destructive tests.

APEX exports are accepted only when the run has two matching identity
observations, a positive operation completion record, `application.apx`,
`.apex/apexlang.json`, and a complete owned tree. `.apx` and APEXlang metadata
are normalized to LF; binaries are hashed byte-for-byte. Production drivers are
SELECT-only and all writes are rejected before SQLcl starts.

The offline portability gate is:

```bash
for script in scripts/*.sh; do bash -n "$script"; done
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v
```
