# Disposable CI and candidate application checks

The required database gate must qualify an exact source SHA on a fresh,
disposable target. It then runs the previous-release upgrade when a previous
artifact exists, records an explicit `NOT_APPLICABLE_INITIAL_RELEASE` result
otherwise, verifies migration history immutability, replays APEX exports and
executes every declared candidate-app check before shared integration.

`ci/runner-contract.json` is a credential-free capability contract. Run its
offline doctor with:

```text
PYTHONPATH=scripts python3 scripts/team.py ci-doctor --contract ci/runner-contract.json
```

The contract names a pinned SQLcl/JDK/APEX/database toolchain, the five
required profile classes and an executable provisioner. The provisioner
interface is deliberately small:

```text
create --run-id UUID --out scratch/ci/UUID
destroy --run-id UUID --instance-token TOKEN
```

The create result is versioned JSON containing a disposable instance token, an
explicit replay environment path, application/workspace fixtures and any
ORDS base URL. Destroy must verify the token and labels before removing one
target. `ci/provisioners/docker_pdb.sh` is a working reference Oracle Free
provider: it downloads the pinned APEX 26.1 archive, verifies its SHA-256,
installs it into the fresh database, creates DEMO, isolated DEMO_META and the
read-only DEMO_VERIFY profile, starts the pinned ORDS image, and returns the
generated workspace identity. Teams may
replace it with a cloned PDB provider without changing the argv contract. It
requires Docker, Oracle Free and ORDS image access, SQLcl 26.2.1+, curl,
enough CPU/RAM/disk for the database and APEX install, and a teardown-capable
runner. To avoid the download in a controlled environment, set
`TEAM_CI_APEX_ARCHIVE` to a reviewed local `apex_26.1.zip`; the provider still
checks the pinned digest. Destroy removes only the exact labeled database,
ORDS container, network, and run-scoped SQLcl aliases.

Candidate declarations under `ci/app-checks/` are version 1 JSON. Each shipped
application needs at least one restricted SELECT dependency assertion and one
declarative authenticated/public page flow. No arbitrary script/eval step or
embedded credential is accepted. A missing runner, fixture, result or required
check is `UNKNOWN`/failure, never a successful skip. The report records source
SHA, replay identity, app/page/check IDs, expected objects and coverage.

The shipped `scripts/ci_replay_runner.py` is a reference adapter: it performs
the identity probes and migration replay through the common SQLcl boundary, and
it deploys/checks tracked applications only when the selected repository has
matching declarations and a qualified SQL/browser adapter. A template with no
tracked applications reports zero-app coverage explicitly. Adopting teams must
replace or extend that adapter for their APEX/ORDS fixture and browser runner;
the default CI job remains fail-closed when a required capability is absent.

Do not run untrusted pull-request code with integration credentials. The
workflow provisions disposable resources and removes only the exact resource
whose token it received. Production connections and credentials are excluded
from CI by contract.

## First run against a new environment

`check-drift` compares live structure against the accepted observed frontier and
exits 3 while no frontier has been adopted. A new database has none, and a
migration run creates one only as a side effect of applying a migration — so a
project with nothing pending can never reach a clean drift check on its own.

Run `team.py adopt-frontier` once, after `setup-state`. It bootstraps the
migration metadata, takes one read-only inventory through the TABLES profile,
records it as an immutable manifest and writes the sequence-0 observation. It is
refused for a production classification, and it does not write any schema object.
