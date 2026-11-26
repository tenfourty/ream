"""Who an agent belongs to, who owns its pages, and how it is shown (agents design, slice 1;
docs/agent/specs/2026-11-26-agents-and-watching-design.md).

An agent is personal (it acts for one person) or shared (it is the deployment's). Today only an
app agent (`mcp_agent`) is personal; a key agent is shared (slice 4 adds `agent_person` for keys).
Nothing is copied: the answers are derived from `mcp_agent`, so an older image after a rollback
reads the same rows.

* `person_of(conn, agent)`: the person the agent belongs to, or None (shared).
* `page_owners(conn, agent)`: who owns that agent's pages: its person, or `shared_owner()`.
* `shown(conn, agent)`: the via line. `Shown.text()` is the only place `", via "` is written
  (`tests/test_via_line.py` refuses any other).
* `owner_sql(agent_col)`: the SQL twin of `page_owners`, read from `mcp_agent` and `service_meta`.

`agent` fields stored or returned anywhere stay the agent's name; `Shown` is for display only.
"""

from __future__ import annotations

from dataclasses import dataclass

from ream import owner, settings, text

NAME_LIMIT = 64


def shared_owner() -> str:
    """Who owns a shared agent's pages: the deployment's owner today (the team's maintainers in
    step 4 of the design). The one function that changes for this question."""
    return settings.owner_id()


def person_of(conn, agent: str) -> str | None:
    """The person `agent` belongs to, or None for a shared agent."""
    row = conn.execute(
        "select person from mcp_agent where agent = %s", (agent,)
    ).fetchone()
    return None if row is None else row[0]


def page_owners(conn, agent: str) -> tuple[str, ...]:
    """Who owns the pages `agent` publishes (and so who a question with no `addressed_to` goes to)."""
    person = person_of(conn, agent)
    return (shared_owner(),) if person is None else (person,)


def owner_sql(agent_col: str) -> str:
    """SQL for who owns the pages of the agent in `agent_col`: the person of a personal agent, else
    the owner the database records (`service_meta`, written by `owner.check`; the SQL twin of
    `page_owners`, whose shared owner is the same id because `owner.check` refuses a start with
    another). No owner id is written into the text and no parameter is added, so the callers'
    positional parameters stay as they are in the long wall queries."""
    return (
        "coalesce((select m.person from mcp_agent m"  # noqa: S608 -- fixed columns and key
        f" where m.agent = {agent_col}),"
        f" (select s.value from service_meta s where s.key = '{owner.META_KEY}'))"
    )


def person_label(person_id: str, stored_name: str | None) -> str:
    """The name shown for a person: `owner_name` for the owner (the rule of `people._person`),
    otherwise their stored name, failing that their id. Sanitised and capped: display only."""
    s = settings.current()
    name = s.owner_name if person_id == s.owner_id else (stored_name or person_id)
    return text.sanitize(name, NAME_LIMIT) or person_id


@dataclass(frozen=True)
class Shown:
    """How an agent is shown. `agent` is the stored name. `person` is the id of the person it
    belongs to (None: shared) and `person_name` their display name; `via` is what it comes through
    (an app's host); `team` is None until step 4."""

    agent: str
    person: str | None = None
    person_name: str = ""
    via: str | None = None
    team: str | None = None

    def text(self) -> str:
        """The line `<person name>, via <host>` for a personal agent, else the agent's own name.
        Escape it where it is drawn."""
        if self.person is not None and self.via:
            return f"{self.person_name}, via {self.via}"
        return self.agent


def app_shown(agent: str, person_id: str, stored_name: str | None, host: str) -> Shown:
    """A person-and-app agent on `host`, for a caller that already holds the pieces."""
    return Shown(agent, person_id, person_label(person_id, stored_name), host)


def owner_app(host: str) -> Shown:
    """The owner's app on `host`, where no agent exists yet (the consent screen the owner reads
    before the app is signed in) or its record is gone."""
    return app_shown("", settings.owner_id(), None, host)


def unknown_app() -> Shown:
    """An answer passed on by an app whose host is no longer known: the owner's, via "an app"."""
    return owner_app("an app")


def shown(conn, agent: str) -> Shown:
    """The via line of `agent`: an app agent is "<its person>, via <host>", any other is its name."""
    row = conn.execute(
        "select m.person, m.host, p.name from mcp_agent m"
        " left join person p on p.id = m.person where m.agent = %s",
        (agent,),
    ).fetchone()
    if row is None:
        return Shown(agent)
    return app_shown(agent, row[0], row[2], row[1])
