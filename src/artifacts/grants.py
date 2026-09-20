"""Named privileges held by a seat.

Deliberately tiny, and deliberately not a role system. There is one grant, it is
read-only, it is off unless someone typed a command, and every check is one
question with one answer: does this seat hold this scope?

The alternative -- a seat that can read everything because it is called
"operator" -- is the shape that turns into an accident later, when a second
operator-ish seat appears and inherits a privilege nobody decided to give it.
"""

from artifacts.db import Database

# Read every open decision across every page, not just this seat's own. Held by
# the operator seat so that "what is waiting on Jeremy" is answerable from the
# service rather than from what each agent remembered to report.
READ_ALL_DECISIONS = "decisions:read-all"

SCOPES = (READ_ALL_DECISIONS,)


class UnknownScope(Exception):
    """A scope that is not one this service defines. Refused at the edge rather
    than stored, so a typo can never sit in the table looking like a privilege
    that is simply never checked."""


def _check(scope: str) -> None:
    if scope not in SCOPES:
        raise UnknownScope(scope)


def grant(db: Database, agent: str, scope: str) -> None:
    _check(scope)
    with db.connect() as conn:
        conn.execute(
            "insert into seat_grant (agent, scope) values (%s, %s)"
            " on conflict (agent, scope) do nothing",
            (agent, scope),
        )


def revoke(db: Database, agent: str, scope: str) -> None:
    """Never refuses an unknown scope: taking a privilege away must work even if
    the scope has since been renamed or removed from SCOPES, or a rename would
    strand rows nobody can delete."""
    with db.connect() as conn:
        conn.execute(
            "delete from seat_grant where agent = %s and scope = %s", (agent, scope)
        )


def has(db: Database, agent: str, scope: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "select 1 from seat_grant where agent = %s and scope = %s", (agent, scope)
        ).fetchone()
    return row is not None


def list_all(db: Database) -> list[tuple[str, str, str]]:
    """Every grant, for `artifacts-admin list-scopes`: a privilege nobody can
    enumerate is a privilege nobody reviews."""
    with db.connect() as conn:
        rows = conn.execute(
            "select agent, scope, granted_at from seat_grant"
            " order by agent asc, scope asc"
        ).fetchall()
    return [(r[0], r[1], r[2].isoformat()) for r in rows]


def for_agent(db: Database, agent: str) -> list[str]:
    """One seat's scopes, for `GET /api/v1/me`.

    Separate from `list_all` rather than filtered from it: this one answers a
    request from the seat itself and must never be able to return another
    seat's row, so the restriction is in the SQL and not in a caller's filter
    that a later edit could drop.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "select scope from seat_grant where agent = %s order by scope asc",
            (agent,),
        ).fetchall()
    return [r[0] for r in rows]
