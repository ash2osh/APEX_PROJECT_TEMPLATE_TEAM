# METADATA backup and restore runbook

METADATA is the release system of record. It holds migration history and
immutable migration members, release version bindings (`TEAM_RELEASE`),
observations, attempts, and mutex state. TABLES and CODE hold the schema state
that this history describes. Recovery must therefore restore a consistent
database point, not just a convenient copy of the release ledger.

## Backup policy

1. **Preferred recovery point:** take and retain a supported RMAN backup for the
   containing database/PDB, including the archived redo needed for recovery.
   Keep the backup off the database host, protect it as production data, and
   rehearse a restore to an isolated database/PDB. Use this for whole-database
   or coordinated METADATA/TABLES/CODE recovery.
2. **Portable logical copy:** use Oracle Data Pump schema mode for the
   configured METADATA, TABLES, and CODE schemas, with a single recorded SCN
   (`FLASHBACK_SCN`) so all three schemas are exported from one consistent
   point. Store the dump and log outside the database host, restrict access,
   encrypt at rest, and retain checksums, the SCN, database/PDB identity, Oracle
   version, export command/parameter file, and completion log. Ensure undo is
   retained long enough for the export. Data Pump's schema mode captures the
   selected schemas' owned objects; it is not a substitute for a complete
   database backup or for reviewing cross-schema dependencies.
3. Back up after a completed release or migration operation when practical.
   Never take a logical backup while an operation is RUNNING, UNKNOWN, or
   holding a recovery mutex. Record the latest accepted schema frontier and
   migration history digest alongside the backup record.

Use a protected Oracle client credential store or interactive prompt. Do not
put passwords in command arguments, shell history, parameter files, or logs.
The Data Pump directory object is a server-side directory; confirm its path,
free space, ownership, and access before starting. Run the following as a
template after replacing the placeholders with the configured schema names and
an approved server-side directory:

```text
expdp /@TEAM_METADATA_PROFILE \
  DIRECTORY=TEAM_BACKUP_DIR \
  DUMPFILE=team_release_%U.dmp \
  LOGFILE=team_release_export.log \
  SCHEMAS=<METADATA_SCHEMA>,<TABLES_SCHEMA>,<CODE_SCHEMA> \
  FLASHBACK_SCN=<RECORDED_SCN>
```

Use an operator-owned protected parameter file if local command-line limits
require one. The directory object and schemas above are deployment-specific;
this template is not executable as written.

## Restore and recovery

1. **Restore first to isolation.** Use RMAN recovery or Data Pump import into an
   isolated clone/PDB with network access disabled. Do not import an old
   METADATA dump over the current shared database as a first diagnostic step.
2. **Establish the recovery point.** Verify the instance/PDB identity, restore
   SCN or timestamp, all expected schemas, dump/backup checksums, and job logs.
   Confirm the METADATA migration history and `TEAM_RELEASE` rows agree with
   the restored TABLES/CODE inventory and its accepted frontier. Inspect
   migration attempts and mutexes; a restored mutex owner is stale until
   worker termination and recovery evidence have been established.
3. **Choose a recovery direction.** If the current database has accepted work
   after the backup point, do not replace only METADATA with the older copy:
   that would make the ledger describe a different TABLES/CODE state. Prefer
   point-in-time recovery of the coordinated database/PDB, or reconcile the
   later work from verified evidence under the migration recovery procedure.
   Preserve the current database and its logs until the recovery owner signs
   off.
4. **Prove the restore.** On the isolated target, verify migration history,
   stored bundle/member checksums, `TEAM_RELEASE` archive digests, and the
   schema inventory/frontier. Confirm a release can be read and verified from
   the restored ledger. Record the commands, target identity, backup IDs,
   resulting SCN, logs, checksum results, discrepancies, and reviewer.
5. **Return to service only through the approved recovery procedure.** Confirm
   the intended recovered database is the one the shared development profiles
   resolve to, then check out registrations and mutexes before resuming work.
   Production remains read-only and is never an automated restore target here.

Data Pump schema import can restore schema-owned objects or remap them, but a
successful import alone does not prove that the METADATA ledger and TABLES/CODE
frontier are mutually consistent. Review Oracle's [Data Pump Export
documentation](https://docs.oracle.com/en/database/oracle/oracle-database/23/sutil/oracle-datapump-export-utility.html),
[Data Pump Import documentation](https://docs.oracle.com/en/database/oracle/oracle-database/23/sutil/oracle-datapump-import-utility.html),
and [RMAN complete recovery guidance](https://docs.oracle.com/en/database/oracle/oracle-database/23/bradv/rman-complete-database-recovery.html)
for the target database release and recovery design.

## Exercise record

The runbook is not considered exercised until an operator restores a backup to
an isolated target, completes the checks above, and records evidence here (or
links a controlled recovery report): date, Oracle version, target identity,
backup ID/checksum, restore point, verification results, discrepancies, and
reviewer. No live exercise is recorded yet.
