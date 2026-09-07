# Destructive schema operations

Files under `operations/zz_*.sql` are maintenance procedures, not migration
bundles. They are excluded from discovery and release artifacts. A destructive
forward migration still declares `-- destructive: true` and requires an exact
target identity, bundle checksum, migration ID, and reviewed confirmation
record. There is no automatic rollback promise for Oracle DDL.

Production maintenance is owner-run. The normal `team.py migrate` and all
bootstrap/adoption/recovery write paths refuse production-classified targets.
