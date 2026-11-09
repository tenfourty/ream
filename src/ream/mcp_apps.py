"""The person-and-app agent and the apps a person lets in over MCP
(docs/agent/specs/2026-11-08-mcp-oauth-design.md, sections 8 and 11.1).

* **`mcp_agent`**: the first consent from an app makes an agent for that person and app,
  named `<person>--<host slug>` (`jeremy--claude-ai`), `-2`, `-3` for a second app on the
  same host. The name passes `keys.AGENT_NAME`, is never the owner id, and never one an
  agent already answers to (a key's, an old name a rename left, or a page's). Never deleted:
  a later consent from the same app reuses it.
* **`mcp_app`**: one person's consent for one app, `app_` plus random, with its
  permissions (a subset of read, write, relay), an idle lapse that slides 30 days and an
  absolute cap 90 days from consent. One live app per person and app.

Wherever an agent is shown, a person-and-app agent reads "<owner name>, via <host>"
(`display_name`); every other agent is shown by its name.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from ream import db as dbmod
from ream import reach, settings
from ream.db import Database
from ream.keys import (
    AGENT_NAME,
    NAME_LOCK_KEY,
    RESERVED_REASON,
    is_reserved_agent_name,
)

APP_PREFIX = "app_"
PERMISSIONS = ("read", "write", "relay")
IDLE_LASTS = timedelta(days=30)
LASTS_AT_MOST = timedelta(days=90)
MAX_AGENT_NAME = 63  # keys.AGENT_NAME: one leading character and up to 62 more
MAX_SUFFIX = (
    99  # -2 .. -99: more apps than that on one host is refused, not numbered on
)

# Serialises choosing a name, so two first consents never pick the same one, and
# a rename or a new key never takes a name one is picking (keys.NAME_LOCK_KEY).
_NAME_LOCK_KEY = NAME_LOCK_KEY


class AgentNameRefused(Exception):
    """No agent name can be made for this person and app: the words say why."""


class AppRefused(Exception):
    """This consent cannot be stored: the words say why."""


@dataclass(frozen=True)
class McpAgent:
    agent: str
    person: str
    client_id: str
    host: str
    created_at: datetime


@dataclass(frozen=True)
class App:
    id: str
    person: str
    client_id: str
    agent: str
    permissions: tuple[str, ...]
    redirect_uri: str
    groups: tuple[str, ...]
    created_at: datetime
    last_used_at: datetime | None
    idle_expires_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None
    events_seen: int

    def live(self, now: datetime | None = None) -> bool:
        """Not revoked, and neither its idle lapse nor its cap has passed."""
        now = now or datetime.now(UTC)
        return (
            self.revoked_at is None
            and now < self.idle_expires_at
            and now < self.expires_at
        )


# --- The name rule --------------------------------------------------------------------------


def host_slug(host: str) -> str:
    """`claude.ai` -> `claude-ai`: lower case, every run of anything but a letter or digit
    one hyphen, none at either end."""
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")


def agent_names(person: str, host: str):
    """The names a person-and-app agent may take, in the order tried: `<person>--<slug>`,
    then `-2` .. `-99`. The slug is shortened, never the person, so every name fits
    `keys.AGENT_NAME`. AgentNameRefused when no name can be made at all."""
    if not AGENT_NAME.fullmatch(person):
        raise AgentNameRefused(
            f"the person id {person!r} is not a name an agent can start with"
        )
    slug = host_slug(host)
    if not slug:
        raise AgentNameRefused(f"the app's address {host!r} has no letters or digits")
    for n in range(1, MAX_SUFFIX + 1):
        suffix = "" if n == 1 else f"-{n}"
        room = MAX_AGENT_NAME - len(person) - 2 - len(suffix)
        cut = slug[:room].rstrip("-")
        if not cut:
            raise AgentNameRefused(
                f"the person id {person!r} leaves no room for the app's address"
            )
        name = f"{person}--{cut}{suffix}"
        if not AGENT_NAME.fullmatch(name):  # pragma: no cover -- built to fit
            raise AgentNameRefused(f"{name!r} is not a valid agent name")
        yield name


def _taken(conn, name: str) -> bool:
    """Whether an agent already answers to `name`: a person-and-app agent, a key's agent,
    an old name a rename left behind, or a page's publisher."""
    for sql in (
        "select 1 from mcp_agent where agent = %s",
        "select 1 from agent_key where agent = %s",
        "select 1 from agent_alias where old = %s",
        "select 1 from artifact where agent = %s",
    ):
        if conn.execute(sql + " limit 1", (name,)).fetchone() is not None:
            return True
    return False


def _agent_row(row) -> McpAgent:
    return McpAgent(row[0], row[1], row[2], row[3], row[4])


_AGENT_COLUMNS = "agent, person, client_id, host, created_at"


def agent_for(
    db: Database, person: str, client_id: str, host: str, now: datetime | None = None
) -> McpAgent:
    """The agent `person` publishes under through the app `client_id`: the one made at the
    first consent, or a new one named by the rule. The name never changes afterwards, even
    if the app's host does."""
    now = now or datetime.now(UTC)
    with db.connect() as conn, conn.transaction():
        conn.advisory_lock(_NAME_LOCK_KEY)
        row = conn.execute(
            f"select {_AGENT_COLUMNS} from mcp_agent"  # noqa: S608 -- fixed columns
            " where person = %s and client_id = %s",
            (person, client_id),
        ).fetchone()
        if row is not None:
            return _agent_row(row)
        for name in agent_names(person, host):
            if is_reserved_agent_name(name):
                raise AgentNameRefused(f"{name!r} is {RESERVED_REASON}")
            if _taken(conn, name):
                continue
            conn.execute(
                "insert into mcp_agent (agent, person, client_id, host, created_at)"
                " values (%s, %s, %s, %s, %s)",
                (name, person, client_id, host, now),
            )
            return McpAgent(name, person, client_id, host, now)
    raise AgentNameRefused(
        f"{person!r} already has {MAX_SUFFIX} apps on {host!r}; no name is left"
    )


def mcp_agent(db: Database, agent: str) -> McpAgent | None:
    """The person-and-app agent called `agent`, or None for any other agent."""
    with db.connect() as conn:
        row = conn.execute(
            f"select {_AGENT_COLUMNS} from mcp_agent where agent = %s",  # noqa: S608 -- fixed columns
            (agent,),
        ).fetchone()
    return None if row is None else _agent_row(row)


# --- How an agent is shown ------------------------------------------------------------------


def via(host: str) -> str:
    """How a person-and-app agent on `host` is shown: `Jeremy, via claude.ai`."""
    return f"{settings.owner_name()}, via {host}"


def display_name(db: Database, agent: str) -> str:
    """How `agent` is shown wherever the gallery or the API shows an agent: a
    person-and-app agent as "<owner name>, via <host>", any other by its name."""
    found = mcp_agent(db, agent)
    return agent if found is None else via(found.host)


# --- Apps (consents) ------------------------------------------------------------------------


_APP_COLUMNS = (
    "id, person, client_id, agent, permissions, redirect_uri, groups, created_at,"
    " last_used_at, idle_expires_at, expires_at, revoked_at, revoked_reason, events_seen"
)


def _app_row(row) -> App:
    return App(
        id=row[0],
        person=row[1],
        client_id=row[2],
        agent=row[3],
        permissions=tuple(row[4]),
        redirect_uri=row[5],
        groups=tuple(row[6]),
        created_at=row[7],
        last_used_at=row[8],
        idle_expires_at=row[9],
        expires_at=row[10],
        revoked_at=row[11],
        revoked_reason=row[12],
        events_seen=int(row[13]),
    )


def create_app(
    db: Database,
    *,
    person: str,
    client_id: str,
    agent: str,
    permissions: list[str] | tuple[str, ...],
    redirect_uri: str,
    groups: list[str] | tuple[str, ...] = (),
    now: datetime | None = None,
) -> App:
    """Store `person`'s consent for the app `client_id`, publishing as `agent` (from
    `agent_for`). AppRefused for a permission outside read, write and relay, an agent that
    is not this pair's, or a live app this pair already has."""
    now = now or datetime.now(UTC)
    unknown = sorted(set(permissions) - set(PERMISSIONS))
    if unknown:
        raise AppRefused(f"unknown permissions: {', '.join(unknown)}")
    granted = [p for p in PERMISSIONS if p in permissions]
    app_id = APP_PREFIX + secrets.token_urlsafe(24)
    with db.connect() as conn, conn.transaction():
        owner = conn.execute(
            "select 1 from mcp_agent where agent = %s and person = %s and client_id = %s",
            (agent, person, client_id),
        ).fetchone()
        if owner is None:
            raise AppRefused(f"{agent!r} is not the agent for this person and app")
        live = conn.execute(
            "select 1 from mcp_app where person = %s and client_id = %s"
            " and revoked_at is null",
            (person, client_id),
        ).fetchone()
        if live is not None:
            raise AppRefused("this person already has this app connected")
        try:
            conn.execute(
                f"insert into mcp_app ({_APP_COLUMNS}) values"  # noqa: S608 -- fixed columns
                " (%s, %s, %s, %s, %s, %s, %s, %s, null, %s, %s, null, null, 0)",
                (
                    app_id,
                    person,
                    client_id,
                    agent,
                    dbmod.jsonb(granted),
                    redirect_uri,
                    dbmod.jsonb(list(groups)),
                    now,
                    now + IDLE_LASTS,
                    now + timedelta(days=settings.current().app_max_lifetime_days()),
                ),
            )
        except dbmod.DUPLICATE_KEY as exc:  # a consent stored at the same moment
            raise AppRefused("this person already has this app connected") from exc
    found = app(db, app_id)
    if found is None:  # pragma: no cover -- just written in its own transaction
        raise AppRefused("the consent was not stored")
    return found


def app(db: Database, app_id: str) -> App | None:
    """The app `app_id`, revoked or lapsed included; None when there is no such app."""
    with db.connect() as conn:
        row = conn.execute(
            f"select {_APP_COLUMNS} from mcp_app where id = %s",  # noqa: S608 -- fixed columns
            (app_id,),
        ).fetchone()
    return None if row is None else _app_row(row)


def live_app(db: Database, person: str, client_id: str) -> App | None:
    """The app `person` has connected from `client_id` and not revoked, or None. Its
    lapse is not checked here: `App.live` says."""
    with db.connect() as conn:
        row = conn.execute(
            f"select {_APP_COLUMNS} from mcp_app"  # noqa: S608 -- fixed columns
            " where person = %s and client_id = %s and revoked_at is null",
            (person, client_id),
        ).fetchone()
    return None if row is None else _app_row(row)


def apps_of(db: Database, person: str) -> list[App]:
    """Every app `person` has connected, newest first, revoked ones included."""
    with db.connect() as conn:
        rows = conn.execute(
            f"select {_APP_COLUMNS} from mcp_app where person = %s"  # noqa: S608 -- fixed columns
            " order by created_at desc, id",
            (person,),
        ).fetchall()
    return [_app_row(r) for r in rows]


class Listed(NamedTuple):
    """An app and how its client calls itself, for the machines page and the admin list."""

    app: App
    host: str
    name: str

    def state(self, now: datetime | None = None) -> str:
        """ "live", "lapsed" (not revoked, but past its idle lapse or cap) or "ended"."""
        if self.app.revoked_at is not None:
            return "ended"
        return "live" if self.app.live(now) else "lapsed"


def listed(db: Database, person: str) -> list[Listed]:
    """Every app `person` has connected, newest first, with its client's host and name."""
    cols = ", ".join("a." + c.strip() for c in _APP_COLUMNS.split(","))
    with db.connect() as conn:
        rows = conn.execute(
            f"select {cols}, c.host, c.name from mcp_app a"  # noqa: S608 -- fixed columns
            " left join mcp_client c on c.id = a.client_id where a.person = %s"
            " order by a.created_at desc, a.id",
            (person,),
        ).fetchall()
    return [Listed(_app_row(r), r[14] or "", r[15] or "") for r in rows]


# --- Reach (section 11.2) -------------------------------------------------------------------


def reach_of(db: Database, app: App) -> reach.Reach:
    """What `app` may reach: every page the owner can read (closed to agents included,
    never the trash); changes to the pages of any of `app.person`'s app agents; relaying
    only with `relay`; and the permissions the person granted (`read` is `ream:read`)."""
    with db.connect() as conn:
        rows = conn.execute(
            "select agent from mcp_agent where person = %s order by agent",
            (app.person,),
        ).fetchall()
    writers = tuple(dict.fromkeys([app.agent, *(row[0] for row in rows)]))
    return reach.Reach(
        agent=app.agent,
        readable=reach.READS_AS_OWNER,
        writable_agents=writers,
        can_relay="relay" in app.permissions,
        permissions=frozenset(f"ream:{p}" for p in app.permissions),
    )
