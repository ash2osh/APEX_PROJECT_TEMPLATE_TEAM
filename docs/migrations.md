# Shared schema migrations

Create migrations offline with `team.py new-migration`; the generated pair is
an immutable SQL member and a SELECT-only verification member. Add exact
dependencies with `team.py add-dependency`. The header is authoritative for
target, destructive status, and dependency checksums.

`migration-plan` is safe to run without an environment file. Developer and
shared integration history may contain foreign applied migrations; strict test
and replay targets require the complete selected history. An unresolved or
unknown attempt blocks later work until its worker has ended and the recorded
evidence is reviewed.

Applying a bundle to the shared schema makes it visible to every developer
immediately. A page exported after that can depend on an unmerged migration, so
the author owns merging the bundle promptly. The shared schema is not the
oracle for candidate readiness: disposable replay and declared app checks are
required before promotion.
