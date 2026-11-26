"""Who may read a page (agents design, slice 2, docs/agent/specs/2026-11-26-agents-and-watching-design.md).

`reads_sql` is the one predicate for "this person's signed-in app may read this page", as a WHERE
clause every store check and listing shares, so they cannot drift apart: a page
of one of the person's personal agents (an app's `mcp_agent.person`, a key's `agent_person.person`; a
personal key also reads its own page through the second, never through its name), or a page open to agents
(`artifact.agents_can_read`), never one in the trash. The owner reads every live page, as the
owner's app always has. A page closed to agents that belongs to another person, or to a shared
agent, is not readable, and the caller answers it as a page that is not there.

Step 4 (private until shared) changes only this function.
"""

from __future__ import annotations

from ream import people


def reads_sql(person: str, table: str = "artifact") -> tuple[str, tuple]:
    """The clause (no `deleted_at` test: the caller has its own) and its parameters."""
    if person == people.owner_person().id:
        return "(1 = 1)", ()
    clause = (
        f"({table}.agent in (select agent from mcp_agent where person = %s"  # noqa: S608 -- `table` is a fixed name from the caller, `person` a placeholder
        " union select agent from agent_person where person = %s)"
        f" or {table}.agents_can_read)"
    )
    return clause, (person, person)
