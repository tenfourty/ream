"""The person-and-app agent and the apps a person lets in over MCP
(docs/agent/specs/2026-11-08-mcp-oauth-design.md, 8 and 11.1): the name rule, its
refusals, the display "<owner name>, via <host>", and apps made directly in the database."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import connect, keys, mcp_apps, rename, settings
from ream.admin import main as admin_main
from ream.app import create_gallery_app

CLAUDE_AI = "https://claude.ai/oauth/claude-ai/client.json"
CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _person() -> str:
    return settings.owner_id()


def test_the_first_consent_names_the_agent_after_the_person_and_host(db):
    made = mcp_apps.agent_for(db, _person(), CLAUDE_AI, "claude.ai", now=NOW)
    assert made.agent == f"{_person()}--claude-ai"
    assert keys.AGENT_NAME.fullmatch(made.agent)
    assert made.host == "claude.ai"
    # The same pair gets the same agent back, even if the app's host has since changed.
    again = mcp_apps.agent_for(db, _person(), CLAUDE_AI, "other.example")
    assert again == made


def test_a_second_app_on_the_same_host_is_numbered(db):
    first = mcp_apps.agent_for(db, _person(), CLAUDE_AI, "claude.ai")
    second = mcp_apps.agent_for(db, _person(), CLAUDE_CODE, "claude.ai")
    third = mcp_apps.agent_for(db, _person(), "reg_abc", "claude.ai")
    assert (first.agent, second.agent, third.agent) == (
        f"{_person()}--claude-ai",
        f"{_person()}--claude-ai-2",
        f"{_person()}--claude-ai-3",
    )


def test_a_name_another_agent_answers_to_is_skipped(db):
    taken = f"{_person()}--claude-ai"
    keys.mint(db, taken)
    made = mcp_apps.agent_for(db, _person(), CLAUDE_AI, "claude.ai")
    assert made.agent == f"{taken}-2"


def test_the_host_becomes_a_slug_and_a_long_one_is_cut_to_fit():
    assert mcp_apps.host_slug("Claude.AI") == "claude-ai"
    assert mcp_apps.host_slug("::1") == "1"
    assert mcp_apps.host_slug("my_app..example.org") == "my-app-example-org"
    names = mcp_apps.agent_names("jeremy", "a" * 40 + "." + "b" * 40)
    first, second = next(names), next(names)
    assert len(first) == len(second) == 63
    assert keys.AGENT_NAME.fullmatch(first) and keys.AGENT_NAME.fullmatch(second)
    assert first.startswith("jeremy--a") and second.endswith("-2")


def test_the_name_rule_refusals(db):
    with pytest.raises(mcp_apps.AgentNameRefused, match="no letters or digits"):
        mcp_apps.agent_for(db, _person(), CLAUDE_AI, "...")
    with pytest.raises(mcp_apps.AgentNameRefused, match="person id"):
        mcp_apps.agent_for(db, "Not A Name", CLAUDE_AI, "claude.ai")
    with pytest.raises(mcp_apps.AgentNameRefused, match="no room"):
        next(mcp_apps.agent_names("p" * 61, "claude.ai"))
    # Never the owner id, even where the rule would build it.
    with (
        settings.override(owner_id="ann--claude-ai"),
        pytest.raises(mcp_apps.AgentNameRefused, match="reserved"),
    ):
        mcp_apps.agent_for(db, "ann", CLAUDE_AI, "claude.ai")
    with db.connect() as conn:
        assert conn.execute("select count(*) from mcp_agent").fetchone()[0] == 0


def test_a_person_and_app_agent_is_shown_as_the_owner_via_the_host(db):
    made = mcp_apps.agent_for(db, _person(), CLAUDE_AI, "claude.ai")
    assert mcp_apps.display_name(db, made.agent) == "Jeremy, via claude.ai"
    assert mcp_apps.display_name(db, made.agent) == (
        f"{settings.owner_name()}, via claude.ai"
    )
    assert mcp_apps.display_name(db, "house") == "house"  # any other agent: its name
    with settings.override(owner_name="Ann"):
        assert mcp_apps.display_name(db, made.agent) == "Ann, via claude.ai"


def test_an_app_made_in_the_database_reads_back(db):
    agent = mcp_apps.agent_for(db, _person(), CLAUDE_AI, "claude.ai").agent
    made = mcp_apps.create_app(
        db,
        person=_person(),
        client_id=CLAUDE_AI,
        agent=agent,
        permissions=["write", "read"],
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        groups=["family"],
        now=NOW,
    )
    assert made.id.startswith(mcp_apps.APP_PREFIX)
    assert made.permissions == ("read", "write")  # stored in the one order
    assert made.groups == ("family",)
    assert made.idle_expires_at == NOW + timedelta(days=30)
    assert made.expires_at == NOW + timedelta(days=90)
    assert made.revoked_at is None and made.events_seen == 0
    assert made.live(NOW) and not made.live(NOW + timedelta(days=31))
    assert mcp_apps.app(db, made.id) == made
    assert mcp_apps.live_app(db, _person(), CLAUDE_AI) == made
    assert mcp_apps.apps_of(db, _person()) == [made]
    assert mcp_apps.app(db, "app_nothing") is None
    assert mcp_apps.live_app(db, _person(), CLAUDE_CODE) is None


def test_an_app_is_refused_a_second_live_consent_bad_permissions_or_a_stranger_agent(
    db,
):
    agent = mcp_apps.agent_for(db, _person(), CLAUDE_AI, "claude.ai").agent
    other = mcp_apps.agent_for(db, _person(), CLAUDE_CODE, "claude.ai").agent
    common = {
        "person": _person(),
        "client_id": CLAUDE_AI,
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
    }
    with pytest.raises(mcp_apps.AppRefused, match="unknown permissions: admin"):
        mcp_apps.create_app(db, agent=agent, permissions=["read", "admin"], **common)
    with pytest.raises(mcp_apps.AppRefused, match="not the agent"):
        mcp_apps.create_app(db, agent=other, permissions=["read"], **common)
    first = mcp_apps.create_app(db, agent=agent, permissions=["read"], **common)
    with pytest.raises(mcp_apps.AppRefused, match="already has this app"):
        mcp_apps.create_app(db, agent=agent, permissions=["read"], **common)
    # Once revoked, the same app may be let in again, under the same agent.
    with db.connect() as conn:
        conn.execute(
            "update mcp_app set revoked_at = %s, revoked_reason = 'revoked' where id = %s",
            (NOW, first.id),
        )
    again = mcp_apps.create_app(db, agent=agent, permissions=["read"], **common)
    assert again.id != first.id and again.agent == first.agent
    assert mcp_apps.live_app(db, _person(), CLAUDE_AI) == again
    assert len(mcp_apps.apps_of(db, _person())) == 2


def _keys_for(db, agent: str) -> int:
    with db.connect() as conn:
        return conn.execute(
            "select count(*) from agent_key where agent = %s", (agent,)
        ).fetchone()[0]


def test_no_key_may_take_a_person_and_app_agents_name(db, monkeypatch):
    held = mcp_apps.agent_for(db, _person(), CLAUDE_AI, "claude.ai").agent
    with pytest.raises(keys.NameHeld, match="person-and-app agent"):
        keys.mint(db, held)
    with pytest.raises(keys.NameHeld, match="person-and-app agent"):
        keys.register(db, held, "a" * 64)
    # artifacts-admin register-key goes through keys.register, and says why.
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    with pytest.raises(SystemExit, match="person-and-app agent"):
        admin_main(["register-key", "--agent", held, "--sha256", "b" * 64])
    assert _keys_for(db, held) == 0
    # Another name still gets its key.
    keys.mint(db, "office")
    assert _keys_for(db, "office") == 1


def test_connecting_a_machine_under_an_app_agents_name_is_refused(db):
    # The name rule's "--" never survives the connect page's name cleaning, so the
    # row is made directly, under a name a person could type there.
    held = "laptop-app"
    with db.connect() as conn:
        conn.execute(
            "insert into mcp_agent (agent, person, client_id, host, created_at)"
            " values (%s, %s, %s, %s, %s)",
            (held, _person(), CLAUDE_AI, "claude.ai", NOW),
        )
    gallery = TestClient(
        create_gallery_app(db), base_url="https://artifacts.tenfourty.site"
    )
    started = gallery.post(
        "/api/v1/connect", json={"name": "laptop"}, headers={"x-real-ip": "203.0.113.5"}
    ).json()
    page = gallery.get("/connect", params={"code": started["code"]}).text
    request_id = re.search(r'name="request_id" value="([^"]+)"', page).group(1)
    refused = gallery.post(
        "/connect/approve",
        data={"request_id": request_id, "code": started["code"], "name": held},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert "person-and-app agent" in refused.text and "Approved" not in refused.text
    # An approval that slipped past the page (the name was taken after it) still
    # never collects a key: the request is denied instead.
    assert connect.approve(db, request_id, held)
    assert connect.collect(db, started["poll_token"]) == {"status": "denied"}
    assert _keys_for(db, held) == 0


def test_names_are_given_under_one_lock(db):
    # A rename, a first consent and a new key's name serialise on the same lock, so
    # a rename onto a name and a first consent picking it cannot both pass.
    assert rename._RENAME_LOCK_KEY == mcp_apps._NAME_LOCK_KEY == keys.NAME_LOCK_KEY
