"""The via line is built in one place, `agents.shown` (agents design, slice 1,
docs/agent/specs/2026-11-26-agents-and-watching-design.md, "The via line", rules 1-7).

A person's app is shown with that person's name, a shared agent with its own; the line is only ever
`<person name>, via <host>` for a personal agent; the person name is sanitised, capped and escaped
where drawn; "your Claude" is only for the reader the app belongs to; and what the owner reads is
byte-identical to what it was before (golden strings below, copied from the code before the change)."""

import ast
import pathlib
from types import SimpleNamespace

import pytest

from ream import agents, gallery, keys, mcp_apps, mcp_clients, oauth, settings

CLIENT = "https://claude.ai/oauth/claude-ai/client.json"
SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "ream"


def _person(db, pid, name):
    with db.connect() as conn:
        conn.execute("insert into person (id, name) values (%s, %s)", (pid, name))


def _line(db, agent):
    with db.connect() as conn:
        return agents.shown(conn, agent)


def test_a_second_persons_app_shows_their_name_not_the_owners(db):
    _person(db, "ann", "Ann Lee")
    ann = mcp_apps.agent_for(db, "ann", CLIENT, "claude.ai").agent
    mine = mcp_apps.agent_for(db, settings.owner_id(), CLIENT, "claude.ai").agent
    assert _line(db, ann).text() == "Ann Lee, via claude.ai"
    assert _line(db, mine).text() == "Jeremy, via claude.ai"
    assert _line(db, ann).agent == ann  # the stored name rides along unchanged


def test_a_person_without_a_name_is_shown_by_their_id(db):
    bob = mcp_apps.agent_for(db, "bob", CLIENT, "claude.ai").agent
    assert _line(db, bob).text() == "bob, via claude.ai"


def test_a_shared_agent_shows_its_name_and_never_a_via_line(db):
    shown = _line(db, "ops")
    assert shown.text() == "ops"
    assert shown.person is None and shown.via is None and shown.team is None
    assert ", via " not in shown.text()


def test_a_name_that_could_forge_a_via_line_is_not_an_agent_name():
    for forged in ("Jeremy, via claude.ai", "ops, via x", "a b", "a,b"):
        assert not keys.AGENT_NAME.fullmatch(forged)


def test_a_hostile_person_name_is_sanitised_capped_and_escaped(db):
    hostile = "Eve\x1b[31m\n<script>alert(1)</script>" + "x" * 200
    _person(db, "eve", hostile)
    eve = mcp_apps.agent_for(db, "eve", CLIENT, "claude.ai").agent
    shown = _line(db, eve)
    assert len(shown.person_name) <= agents.NAME_LIMIT
    assert "\x1b" not in shown.text() and "\n" not in shown.text()
    assert shown.text().endswith(", via claude.ai")
    row = SimpleNamespace(recorded_by=eve, asked_by=eve)
    _, recorder = gallery._passed_on(row, {eve: shown})
    assert "<script>" not in recorder and "&lt;script&gt;" in recorder
    asker = gallery._asker_line(row, {eve: shown}, "jeremy")
    assert "<script>" not in asker


def test_your_claude_is_only_for_the_reader_the_app_belongs_to(db):
    _person(db, "ann", "Ann Lee")
    ann = mcp_apps.agent_for(db, "ann", CLIENT, "claude.ai").agent
    mine = mcp_apps.agent_for(db, "jeremy", CLIENT, "claude.ai").agent
    hosts = {ann: _line(db, ann), mine: _line(db, mine)}
    mine_row = SimpleNamespace(asked_by=mine)
    ann_row = SimpleNamespace(asked_by=ann)
    assert "From your Claude (claude.ai)," in gallery._asker_line(
        mine_row, hosts, "jeremy"
    )
    assert "From your Claude (claude.ai)," in gallery._asker_line(ann_row, hosts, "ann")
    assert "From Ann Lee’s Claude (claude.ai)," in gallery._asker_line(
        ann_row, hosts, "jeremy"
    )
    assert "From Jeremy’s Claude (claude.ai)," in gallery._asker_line(
        mine_row, hosts, "ann"
    )


def test_the_owners_served_text_is_byte_identical_golden(db):
    mine = mcp_apps.agent_for(db, settings.owner_id(), CLIENT, "claude.ai").agent
    hosts = {mine: _line(db, mine)}
    row = SimpleNamespace(asked_by=mine, recorded_by=mine)
    assert gallery._passed_on(row, hosts) == ("claude.ai", "Jeremy, via claude.ai")
    assert gallery._passed_on(SimpleNamespace(recorded_by="gone"), hosts) == (
        "an app",
        "Jeremy, via an app",
    )
    assert gallery._asker_line(row, hosts, "jeremy") == (
        '<p class="qasker">From your Claude (claude.ai), not this page’s agent. '
        "Your answer goes back to it.</p>"
    )
    # an asker that is not (or no longer) an app is shown by its stored name
    assert gallery._asker_line(SimpleNamespace(asked_by="gone"), hosts, "jeremy") == (
        '<p class="qasker">From your Claude (gone), not this page’s agent. '
        "Your answer goes back to it.</p>"
    )
    assert gallery._asker_line(SimpleNamespace(asked_by=None), hosts, "jeremy") == ""


def test_the_consent_page_says_the_owners_line_byte_for_byte_golden():
    app = mcp_clients.Client(
        id=CLIENT,
        kind="cimd",
        host="claude.ai",
        name="Claude",
        redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
    )
    page = oauth._consent_page(app, "n", "https://claude.ai/api/mcp/auth_callback", [])
    assert (
        page.body.decode().count(
            '<p><label><input type="checkbox" name="perm" value="write">'
            " Publish, comment and ask, signed \u201cJeremy, via claude.ai\u201d</label></p>"
        )
        == 1
    )


def test_the_connected_apps_list_shows_the_owners_line(db):
    mine = mcp_apps.agent_for(db, settings.owner_id(), CLIENT, "claude.ai")
    app = SimpleNamespace(agent=mine.agent, person=settings.owner_id())
    assert gallery._app_line(app, "claude.ai") == "Jeremy, via claude.ai"


def test_only_agents_writes_a_via_line():
    """`", via "` is a string in code nowhere but `agents.py` (docstrings and comments aside);
    `via session` is a session label, not a person's line."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "agents.py":
            continue
        tree = ast.parse(path.read_text())
        docs = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(
                n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docs
                and ", via " in node.value
                and ", via session" not in node.value
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"a via line is written outside agents.shown: {offenders}"


@pytest.mark.parametrize("agent", ["ops", "jeremy--claude-ai"])
def test_shown_never_changes_the_agent_field(db, agent):
    assert _line(db, agent).agent == agent
