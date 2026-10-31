# Live feature-release checks

`tests/test_live_feature_release.py` runs the feature-release path against a
real Oracle schema. It is opt-in: CI never runs it, and it skips unless the
variables below are set.

## What you need

- SQLcl on `PATH`, and a saved SQLcl connection whose login is the schema owner.
- A schema you can afford to lose. The checks create and drop their own objects,
  all named `LIVE_<random>` or `live<random>`. They touch nothing else.
- Bash. The feature path runs `scripts/team.sh`, so the module is skipped on Windows.

## Run

```bash
TEMPLATE_LIVE_DISPOSABLE=1 \
TEMPLATE_LIVE_CONNECTION=<saved-connection> \
TEMPLATE_LIVE_SCHEMA=<SCHEMA> \
python3 -m unittest discover -s tests -p test_live_feature_release.py -v
```

For the two-schema check, also set `TEMPLATE_LIVE_SECOND_CONNECTION` and
`TEMPLATE_LIVE_SECOND_SCHEMA`. Without them that one check is skipped.

To run the staging profile on a separate database, such as a local Docker
container, also set `TEMPLATE_LIVE_STAGING_CONNECTION`. It names a saved
connection that logs in as the same schema there. The staging profile then uses
it, and the production profile keeps `TEMPLATE_LIVE_CONNECTION`. Do not combine
it with the two-schema variables.

The whole module takes about three to four minutes against a local container.

## Safety

- Each run copies this repository to a temporary directory and works there. It
  does not write to your checkout.
- The product's production-name guard applies. A connection name containing a
  word such as `live`, `prod` or `prd` is refused, so name the connection and
  schema so they are clearly disposable. The module's own object names are not
  checked.
- Teardown drops every object the run created and reports any drop that fails.
  A scenario that fails early drops only what it created.
- Failed runs can leave a rollout lock in the copy's `releases/.local-state.json`.
  The copy is removed at the end, so the lock does not outlive the run.

## Scenarios covered

| Check | What it shows |
| --- | --- |
| `test_capture_freeze_then_staging_and_production_with_stage_proof` | Capture from DEV, freeze, a staging apply with checks, production with stage proof, object coverage, and cleanup eligibility. |
| `test_production_refuses_without_a_capsule_or_stage_proof` | Production refuses without a capsule or stage proof, and writes nothing either way. |
| `test_ddl_with_a_missing_prerequisite_is_refused_at_prepare` | A missing prerequisite table stops preparation before any write. |
| `test_abrupt_termination_mid_apply_is_recovered_after_reconciliation` | A SIGKILL mid-apply, then reconciliation, then recovery to success. |
| `test_noop_checkpoint_is_refused_while_an_unresolved_attempt_occupies_the_scope` | An unchanged staging unit is a no-op. An unresolved attempt on the same target still refuses it at the checkpoint level, and nothing is written. |
| `test_two_schemas_on_one_database_apply_as_two_checkpoints` | Two schemas on one database, applied as two checkpoints. Needs the second schema. |

## Not covered

- A migration profile that differs from the read profile. That was checked by
  hand with a proxy login.
- Oracle versions other than the one you run against.
- Real staging or production targets. These checks never touch them. The
  feature-release guide requires a separately authorized disposable rehearsal
  before any measured claim.

## The kill check and the database's timing

The kill check waits until the killed run reaches `attempting`, kills its process
group, then waits until the package body's DDL time and status stop changing for
ten seconds. That wait matters because killing the client does not stop a DDL
the server has already started. Recovery must not capture a state that is still
moving.

The ten-second quiet period is a heuristic, not proof. The schema login cannot
read `V$SESSION`, so the check cannot see whether the server-side operation has
ended. Before reconciling a real interrupted rollout, confirm the operation has
ended with a privileged view, as the reconciliation prompt asks.
