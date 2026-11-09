"""`artifacts-admin`: key tooling for the artifacts service.

A bearer token must never pass through a terminal, a transcript, a log, or a
network hop. `new-key` is built around that: it generates the token WHERE IT
WILL BE USED (an agent's own machine, or wherever the operator runs it right
before a `kubectl exec`), needs no database at all, and writes the token
straight to a file that only its owner can read. From there on, only the
token's SHA-256 ever travels anywhere -- typed into `register-key`, piped
over `kubectl exec`, or printed by `list-keys`. Neither `new-key`'s own
output nor any other command in this module ever prints the token itself.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ream import grants, keys
from ream.cli import prog_name

# The database modules are imported by the commands that use them, never here:
# `new-key` needs no database and must run where only the client is installed
# (the uv tool has no psycopg; found minting retrobrew_maintainer, 7 Oct 2026).
if TYPE_CHECKING:
    from ream.db import Database

_AGENT_NAME = keys.AGENT_NAME
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _check_agent(agent: str) -> None:
    if not _AGENT_NAME.fullmatch(agent):
        raise SystemExit(
            f"invalid agent name {agent!r}: must match {_AGENT_NAME.pattern!r}"
        )
    if keys.is_reserved_agent_name(agent):
        raise SystemExit(f"invalid agent name {agent!r}: {keys.RESERVED_REASON}")


def _current(db: Database, agent: str) -> str:
    """The agent a typed name means now, saying so when it was renamed."""
    from ream import rename

    now = rename.resolve(db, agent)
    if now != agent:
        print(f"{agent} was renamed to {now}; using {now}", file=sys.stderr)
    return now


def _open_db() -> Database:
    from ream.db import Database

    return Database(_database_url())


def _database_url() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")
    return dsn


def cmd_new_key(args: argparse.Namespace) -> None:
    _check_agent(args.agent)
    # Matches keys.mint()'s own format exactly -- an agent registered by hand
    # through this CLI must verify identically to one minted in-process.
    token = "art_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()

    path = args.out
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)  # explicit: umask must never weaken this

    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(
            f"refusing to overwrite an existing key file: {path}"
        ) from None
    try:
        os.write(fd, token.encode())  # no trailing newline: the file IS the token
    finally:
        os.close(fd)

    print(f"agent: {args.agent}")
    print(f"path: {path}")
    print(f"sha256={digest}")
    print(
        "kubectl -n artifacts exec deploy/artifacts -- artifacts-admin "
        f"register-key --agent {args.agent} --sha256 {digest}"
    )


def cmd_register_key(args: argparse.Namespace) -> None:
    _check_agent(args.agent)
    if not _SHA256_HEX.fullmatch(args.sha256):
        raise SystemExit("sha256 must be exactly 64 lowercase hex characters")
    db = _open_db()
    args.agent = _current(db, args.agent)
    try:
        key_id = keys.register(db, args.agent, args.sha256)
    except keys.NameHeld as exc:
        raise SystemExit(str(exc)) from None
    print(f"key_id={key_id} agent={args.agent}")


def cmd_revoke_key(args: argparse.Namespace) -> None:
    db = _open_db()
    rows = keys.revoke(db, args.key_id)
    if rows == 0:
        raise SystemExit(f"no active key with id {args.key_id}")
    agent = keys.agent_of(db, args.key_id)
    print(f"revoked key_id={args.key_id} agent={agent}")


def cmd_list_keys(_args: argparse.Namespace) -> None:
    db = _open_db()
    for row in keys.list_keys(db):
        print(f"{row.id}\t{row.agent}\t{row.created_at}\t{row.revoked_at or '-'}")


def cmd_outbox(_args: argparse.Namespace) -> None:
    """The core outbox, read-only: counts by sink, never an event."""
    from ream import sinks

    report = sinks.status(_open_db())
    names = sorted({*report["pending"], *report["parked"], *report["delivered"]})
    if not names:
        print("outbox: empty")
        return
    for name in names:
        print(
            f"{name}\tpending={report['pending'].get(name, 0)}"
            f"\tparked={report['parked'].get(name, 0)}"
            f"\tdelivered={report['delivered'].get(name, 0)}"
        )


def cmd_partner_outbox(args: argparse.Namespace) -> None:
    from ream import partner

    db = _open_db()
    if args.retry and args.retry_parked:
        raise SystemExit("give --retry <event_id> or --retry-parked, not both")
    if args.retry or args.retry_parked:
        released = partner.unpark(db, args.retry)
        print(f"released {released} parked event(s)")
        if args.retry and released == 0:
            raise SystemExit(f"no parked event with id {args.retry}")
    report = partner.status(db)

    def secs(value):
        return "-" if value is None else f"{value}s"

    print(f"partner bridge: {'on' if report['enabled'] else 'off in this shell'}")
    print(f"undelivered events: {report['depth']}")
    print(f"oldest undelivered: {secs(report['oldest_undelivered_age_seconds'])}")
    print(
        f"parked: {report['parked']}"
        f" (oldest {secs(report['oldest_parked_age_seconds'])},"
        f" codes {', '.join(report['parked_codes']) or '-'})"
    )
    if report["auth_errors"]:
        print(
            f"WARNING: {report['auth_errors']} waiting event(s) last refused with"
            " 401/403: check the signing key, its key_id and the clock"
        )
    print(
        f"last error of a waiting event: {report['last_error'] or '-'}"
        f" ({secs(report['last_error_age_seconds'])} ago)"
    )


def cmd_grant_scope(args: argparse.Namespace) -> None:
    _check_agent(args.agent)
    db = _open_db()
    args.agent = _current(db, args.agent)
    try:
        grants.grant(db, args.agent, args.scope)
    except grants.UnknownScope:
        raise SystemExit(
            f"unknown scope {args.scope!r}: this service defines {list(grants.SCOPES)}"
        ) from None
    print(f"granted {args.scope} to {args.agent}")


def cmd_revoke_scope(args: argparse.Namespace) -> None:
    _check_agent(args.agent)
    db = _open_db()
    args.agent = _current(db, args.agent)
    grants.revoke(db, args.agent, args.scope)
    # Says what is true afterwards rather than what it did: "revoked" on a grant
    # that was never held would read as "it used to have this", which is a
    # different and alarming fact.
    print(f"{args.agent} does not hold {args.scope}")


def cmd_list_scopes(_args: argparse.Namespace) -> None:
    rows = grants.list_all(_open_db())
    if not rows:
        print("no seat holds any scope")
    for agent, scope, granted_at in rows:
        print(f"{agent}\t{scope}\t{granted_at}")


def cmd_rename_agent(args: argparse.Namespace) -> None:
    """Rename an agent, or merge it into one that already exists. Every row
    that names it moves, its keys keep working as the new name, and the old
    name still means the new one wherever somebody types it."""
    from ream import rename

    db = _open_db()
    try:
        counts = rename.rename_agent(
            db,
            args.old,
            args.new,
            dry_run=args.dry_run,
            release_name=args.release_name,
        )
    except rename.Refused as exc:
        raise SystemExit(f"refused: {exc}") from None
    if args.dry_run:
        print(
            f"DRY RUN -- nothing changed. Renaming {args.old} to {args.new} would change:"
        )
    else:
        print(f"renamed {args.old} to {args.new}:")
    for column, n in counts.items():
        print(f"{column}\t{n}")
    total = sum(counts.values())
    print(f"total\t{total}{' (DRY RUN, nothing changed)' if args.dry_run else ''}")
    if args.release_name:
        verb = "would be" if args.dry_run else "was"
        print(f"alias: none {verb} written; {args.old} is free to name a new machine")


def cmd_release_name(args: argparse.Namespace) -> None:
    """Delete an alias so the old name is free again."""
    from ream import rename

    try:
        target = rename.release_alias(_open_db(), args.alias, dry_run=args.dry_run)
    except rename.Refused as exc:
        raise SystemExit(f"refused: {exc}") from None
    if args.dry_run:
        print(
            f"DRY RUN -- nothing changed. {args.alias} (an old name of {target}) "
            "would be released: 1 alias row deleted."
        )
    else:
        print(
            f"released {args.alias} (it meant {target}): 1 alias row deleted. "
            "It is free to name a new machine."
        )


def cmd_change_owner(args: argparse.Namespace) -> None:
    """Move the database to another owner id: every column the owner's own acts were
    stored under is rewritten in one transaction, and the new id is recorded."""
    from ream import owner

    db = _open_db()
    try:
        before = owner.stored(db)
        counts = owner.change_owner(db, args.new, dry_run=args.dry_run)
    except owner.Refused as exc:
        raise SystemExit(f"refused: {exc}") from None
    if args.dry_run:
        print(
            f"DRY RUN -- nothing changed. Changing the owner from {before} to {args.new}"
            " would change:"
        )
    else:
        print(f"changed the owner from {before} to {args.new}:")
    for column, n in counts.items():
        print(f"{column}\t{n}")
    total = sum(counts.values())
    print(f"total\t{total}{' (DRY RUN, nothing changed)' if args.dry_run else ''}")
    if not args.dry_run:
        print(
            f"now set REAM_OWNER_ID={args.new} on the service and restart it: it"
            " refuses to start while the setting and the database disagree"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog_name("artifacts-admin"),
        description="Key and database tooling for Ream (formerly artifacts). Also run as `ream admin`.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    new_key = sub.add_parser(
        "new-key", help="generate a token on this machine; needs no database"
    )
    new_key.add_argument("--agent", required=True)
    new_key.add_argument("--out", required=True)
    new_key.set_defaults(func=cmd_new_key)

    register_key = sub.add_parser(
        "register-key", help="register a token's SHA-256 (needs DATABASE_URL)"
    )
    register_key.add_argument("--agent", required=True)
    register_key.add_argument("--sha256", required=True)
    register_key.set_defaults(func=cmd_register_key)

    revoke_key = sub.add_parser("revoke-key")
    revoke_key.add_argument("key_id")
    revoke_key.set_defaults(func=cmd_revoke_key)

    list_keys_parser = sub.add_parser("list-keys")
    list_keys_parser.set_defaults(func=cmd_list_keys)

    partner_outbox = sub.add_parser(
        "partner-outbox",
        help="partner bridge: undelivered events, oldest age, last error (needs DATABASE_URL)",
    )
    partner_outbox.add_argument(
        "--retry", metavar="EVENT_ID", help="release one parked event for another try"
    )
    partner_outbox.add_argument(
        "--retry-parked", action="store_true", help="release every parked event"
    )
    partner_outbox.set_defaults(func=cmd_partner_outbox)

    outbox = sub.add_parser(
        "outbox",
        help="core outbox: pending, parked and delivered counts by sink (read-only; needs DATABASE_URL)",
    )
    outbox.set_defaults(func=cmd_outbox)

    grant_scope = sub.add_parser(
        "grant-scope", help="give a seat a named privilege (needs DATABASE_URL)"
    )
    grant_scope.add_argument("--agent", required=True)
    grant_scope.add_argument("--scope", required=True, choices=list(grants.SCOPES))
    grant_scope.set_defaults(func=cmd_grant_scope)

    revoke_scope = sub.add_parser("revoke-scope")
    revoke_scope.add_argument("--agent", required=True)
    # No `choices` here, on purpose: a privilege must be removable even after the
    # service stops defining it, or a renamed scope leaves rows nobody can delete.
    revoke_scope.add_argument("--scope", required=True)
    revoke_scope.set_defaults(func=cmd_revoke_scope)

    list_scopes = sub.add_parser("list-scopes")
    list_scopes.set_defaults(func=cmd_list_scopes)

    rename_agent = sub.add_parser(
        "rename-agent",
        help="rename an agent, or merge it into an existing one (needs DATABASE_URL)",
        description="Rename an agent, or merge it into an existing one. By default the old "
        "name is kept as an alias, so a name somebody types still means the agent it became; "
        "that also means a new machine connecting as the old name would get a key for the new "
        "one. Pass --release-name to leave no alias and keep the old name free.",
    )
    rename_agent.add_argument("old")
    rename_agent.add_argument("new")
    rename_agent.add_argument(
        "--release-name",
        action="store_true",
        help="leave no alias, so the old name is free for a new machine "
        "(with --dry-run, says the alias would not be written)",
    )
    rename_agent.add_argument(
        "--dry-run", action="store_true", help="count what would change, change nothing"
    )
    rename_agent.set_defaults(func=cmd_rename_agent)

    change_owner = sub.add_parser(
        "change-owner",
        help="move the database to another owner id (needs DATABASE_URL)",
        description="Change the owner id the database was written for: rewrite every "
        "column the owner's own acts were stored under (comment author and resolver, "
        "decision answerer, event actor, picture stager, folder owner) in one "
        "transaction and record the new id. Then set REAM_OWNER_ID to it on the "
        "service. Refused if the id is an agent's name. An answer's stored JSON record "
        "and the partner outbox keep the id they were written with.",
    )
    change_owner.add_argument("new", help="the new owner id")
    change_owner.add_argument(
        "--dry-run", action="store_true", help="count what would change, change nothing"
    )
    change_owner.set_defaults(func=cmd_change_owner)

    release_name = sub.add_parser(
        "release-name",
        help="free an old name: delete the alias that makes it mean another agent "
        "(needs DATABASE_URL)",
    )
    release_name.add_argument(
        "alias", help="the old name; refused if it is not an alias"
    )
    release_name.add_argument(
        "--dry-run",
        action="store_true",
        help="say what would be released, change nothing",
    )
    release_name.set_defaults(func=cmd_release_name)

    purge = sub.add_parser(
        "purge-deleted",
        help="destroy pages whose thirty-day recovery window has run out",
    )
    purge.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would go, and remove nothing",
    )
    purge.set_defaults(func=cmd_purge_deleted)

    pack = sub.add_parser(
        "pack-versions",
        help="store old versions as zstd deltas, verified, and blank their plain bodies",
    )
    pack.set_defaults(func=cmd_pack_versions)

    agents = sub.add_parser(
        "closed-to-agents",
        help="list pages closed to other agents; optionally open all but some",
    )
    agents.add_argument(
        "--open-all-except",
        metavar="IDS",
        help="comma-separated ids to KEEP closed; every other closed page is opened",
    )
    agents.add_argument(
        "--dry-run", action="store_true", help="say what would open, change nothing"
    )
    agents.set_defaults(func=cmd_closed_to_agents)

    for name, src_help, dst_help, func in (
        (
            "migrate-from-postgres",
            "the Postgres DSN, or env:NAME to read it from an environment variable",
            "the SQLite file to create (a path, or sqlite:///path)",
            cmd_migrate_from_postgres,
        ),
        (
            "migrate-to-postgres",
            "the SQLite file (a path, or sqlite:///path)",
            "the Postgres DSN, or env:NAME to read it from an environment variable",
            cmd_migrate_to_postgres,
        ),
    ):
        migrate = sub.add_parser(
            name,
            help="copy every table into an empty database and verify the copy"
            " (run with the service stopped)",
        )
        migrate.add_argument("--from", dest="source", required=True, help=src_help)
        migrate.add_argument("--to", dest="destination", required=True, help=dst_help)
        migrate.set_defaults(func=func)

    drill = sub.add_parser(
        "restore-drill",
        help="restore a nightly snapshot into a scratch file, verified, and count its rows",
    )
    drill.add_argument(
        "--snapshot", required=True, help="an artifacts-sqlite-*.db file"
    )
    drill.set_defaults(func=cmd_restore_drill)

    return parser


def cmd_restore_drill(args) -> None:
    """The restore drill (spec §5): the snapshot passes SQLite's integrity
    check, then the migration copier restores it into a scratch file and
    verifies it table by table, printing each table's row count. The scratch
    folder (under SQLITE_TMPDIR when set) is removed afterwards; the snapshot
    itself is only read, through a copy."""
    import shutil
    import sqlite3
    import tempfile

    from ream import migrate
    from ream.db import Database

    if not os.path.isfile(args.snapshot):
        raise SystemExit(f"no such snapshot: {args.snapshot}")
    with tempfile.TemporaryDirectory(
        prefix="restore-drill-", dir=os.environ.get("SQLITE_TMPDIR")
    ) as scratch:
        source = os.path.join(scratch, "snapshot.db")
        shutil.copyfile(args.snapshot, source)
        check = sqlite3.connect(source)
        try:
            (result,) = check.execute("pragma integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            result = str(exc)
        finally:
            check.close()
        if result != "ok":
            raise SystemExit(
                f"REFUSED: the snapshot fails its integrity check: {result}"
            )
        print("integrity_check ok")
        restored = Database(f"sqlite:///{os.path.join(scratch, 'restored.db')}")
        try:
            checks = migrate.copy(
                Database(f"sqlite:///{source}"), restored, log=lambda _: None
            )
        except migrate.Mismatch as exc:
            raise SystemExit(f"REFUSED: {exc}") from None
    for table in checks:
        print(f"{table.name}\t{table.rows}")
    print(
        f"restored and verified {len(checks)} tables, {sum(c.rows for c in checks)} rows"
    )


def _postgres_dsn(value: str) -> str:
    """A Postgres DSN from the command line, or from the environment variable
    `env:NAME` names, so a password need never be a process argument."""
    if value.startswith("env:"):
        dsn = os.environ.get(value.removeprefix("env:"), "")
        if not dsn:
            raise SystemExit(f"{value.removeprefix('env:')} is not set")
        value = dsn
    if not value.startswith(("postgresql://", "postgres://")):
        raise SystemExit("not a Postgres DSN: expected postgresql://...")
    return value


def _sqlite_dsn(value: str) -> str:
    if value.startswith("sqlite:"):
        return value
    if "://" in value:
        raise SystemExit("not a SQLite path: expected a file path or sqlite:///path")
    return f"sqlite:///{value}"


def _run_migration(source: str, destination: str) -> None:
    from ream import migrate
    from ream.db import Database

    print(
        f"copying {migrate.redact(source)} -> {migrate.redact(destination)}",
        file=sys.stderr,
    )
    try:
        migrate.copy(Database(source), Database(destination))
    except migrate.Mismatch as exc:
        raise SystemExit(
            f"REFUSED, nothing was kept in the destination: {exc}"
        ) from None


def cmd_migrate_from_postgres(args) -> None:
    """Postgres to SQLite, the cutover (spec §5)."""
    _run_migration(_postgres_dsn(args.source), _sqlite_dsn(args.destination))


def cmd_migrate_to_postgres(args) -> None:
    """SQLite back to Postgres, the rollback that keeps writes made on SQLite."""
    _run_migration(_sqlite_dsn(args.source), _postgres_dsn(args.destination))


def cmd_closed_to_agents(args) -> None:
    """The one-off step behind Jeremy's 2a (homelab-ij8.105): existing pages
    shipped CLOSED so he could see the list and pick which stay closed before
    the rest open. Without --open-all-except it only lists. With it, every
    closed page NOT named is opened, and each one is printed, because a bulk
    change that says only a number leaves nobody able to check it."""
    from ream import store

    database = _open_db()
    with database.connect() as conn:
        closed = conn.execute(
            "select id, agent, title from artifact"
            " where not agents_can_read and deleted_at is null order by agent, title"
        ).fetchall()
    if args.open_all_except is None:
        for row in closed:
            print(f"{row[0]}\t{row[1]}\t{row[2]}")
        print(f"{len(closed)} closed")
        return
    keep = {x.strip() for x in args.open_all_except.split(",") if x.strip()}
    unknown = keep - {row[0] for row in closed}
    if unknown:
        # Refuse rather than guess: a mistyped id would otherwise mean a page he
        # wanted closed quietly opens.
        raise SystemExit(
            f"not a closed page: {', '.join(sorted(unknown))}; nothing changed"
        )
    to_open = [row for row in closed if row[0] not in keep]
    for row in to_open:
        print(
            f"{'would open' if args.dry_run else 'opened'}\t{row[0]}\t{row[1]}\t{row[2]}"
        )
    if not args.dry_run:
        for row in to_open:
            store.set_agents_can_read(database, row[0], True, agent=None)
    print(
        f"{len(to_open)} {'would open' if args.dry_run else 'opened'}, {len(keep)} kept closed"
    )


def cmd_purge_deleted(args) -> None:
    """Make the thirty days real.

    Jeremy chose a recoverable delete partly because it is honest about
    eventually being gone. A soft delete that nothing ever sweeps is not a
    thirty-day window, it is a hidden row for ever -- and this service already
    has one feature that is built, tested and called by nothing at all, which
    is exactly the failure this command exists not to repeat.

    Prints the id of everything it destroyed, one per line, because this is the
    only operation here that cannot be undone and a log line saying "3" gives
    whoever reads it no way to find out afterwards what went.
    """
    from ream import store

    database = _open_db()
    if args.dry_run:
        # The same cutoff the real run uses, read the same way, so the rehearsal
        # cannot disagree with the performance.
        due = [
            row
            for row in store.list_deleted(database)
            if row.deleted_at is not None
            and row.deleted_at.replace(tzinfo=row.deleted_at.tzinfo or UTC)
            + store.DELETE_WINDOW
            <= datetime.now(UTC)
        ]
        for row in due:
            print(f"would destroy {row.id}\t{row.agent}\t{row.title}")
        print(f"{len(due)} due")
        return
    gone = store.purge_expired(database)
    for artifact_id in gone:
        print(f"destroyed {artifact_id}")
    print(f"{len(gone)} destroyed")


def cmd_pack_versions(_args) -> None:
    """Pack every version written before deltas existed, verify each by rebuilding it, and
    only then blank the old bodies (docs/agent/specs/2026-10-26-delta-versions-design.md).
    Idempotent and resumable. Space comes back only on the next VACUUM."""
    from ream import repack

    report = repack.pack_versions(_open_db())
    print(
        f"{report.pages} pages: {report.packed} packed, {report.blanked} blanked, "
        f"{report.failed} failed, {report.saved} bytes saved"
    )
    if report.failed:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    args.func(args)


if __name__ == "__main__":
    main()
