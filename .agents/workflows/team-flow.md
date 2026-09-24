# Team workflow

1. **Builder route:** Edit in APEX Builder, run `scripts/team.sh export-app <alias>`, review `git status` and diff, and commit. There is no import in the normal Builder loop.
2. **File-first / Agent route:** Edit `apps/<alias>/`, review, and commit. Prepare publish with `scripts/team.sh prepare-publish <alias> --ref HEAD`. Post the printed app-scoped pause notice and obtain explicit teammate checkout acknowledgements. Publish with `scripts/team.sh publish-app --prepared <id> --confirm-pause --ack <alias>:<uuid>`. Only selected apps pause.
3. **Schema work:** Author migration pairs under `migrations/`, check drift, apply through the isolated METADATA profile, and merge promptly.
4. **Promotion:** Build kind-bound immutable archives (`schema/v<semver>` once for shared migrations; `app/<alias>/v<semver>` for single application releases). Test on protected test target and hand off an offline production-owner runbook. Production writes remain refused.
