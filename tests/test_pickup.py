"""An agent's own "when I act on an answer" line (docs/agent/specs/
2026-10-29-where-an-answer-goes.md, section 6): set, clear, scoping, validation,
rendering and escaping, rename, the API key and the CLI."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_client import key_file, live  # noqa: F401
from test_listening import _ask, _deliver, _run, auth, bridge, client  # noqa: F401

from artifacts import events, keys, listening, onboarding, rename
from artifacts.app import create_gallery_app


def _put(client, token, text):
    return client.put(
        "/api/v1/listening/pickup", json={"pickup": text}, headers=auth(token)
    )


def _seen(db, agent="cove", session="cove/1", ago=timedelta(minutes=5)):
    """A reader of this agent who last asked for its events `ago` ago."""
    events.note_read(db, agent, session)
    with db.connect() as conn:
        conn.execute(
            "update seat_read set last_read_at = %s where agent = %s and session = %s",
            (datetime.now(UTC) - ago, agent, session),
        )


# --- the rules on the text ---------------------------------------------------


def test_clean_keeps_a_plain_line_and_collapses_spaces():
    assert listening.clean_pickup("  within   the hour ") == "within the hour"


def test_clean_strips_control_and_format_characters():
    dirty = "within\x1b[31m the\u200b hour\u202e\x07"
    assert listening.clean_pickup(dirty) == "within[31m the hour"


@pytest.mark.parametrize("sep", ["\n", "\r", "\r\n", "\x0b", "\x0c", "\x85", " ", " "])
def test_clean_refuses_a_line_break_of_any_kind(sep):
    with pytest.raises(listening.BadPickup, match="single line"):
        listening.clean_pickup(f"a{sep}b")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\u200b\x1b",
        "\u3164",
        "\u3164 \u200b",
        "\u115f\u1160\uffa0",
        "\u2800",
        "\u00a0\u3000",
    ],
)
def test_clean_refuses_an_empty_line(text):
    with pytest.raises(listening.BadPickup, match="empty"):
        listening.clean_pickup(text)


def test_clean_caps_at_the_limit():
    assert listening.clean_pickup("x" * listening.PICKUP_LIMIT)
    with pytest.raises(listening.BadPickup, match="at most"):
        listening.clean_pickup("x" * (listening.PICKUP_LIMIT + 1))


def test_clean_refuses_a_non_string():
    with pytest.raises(listening.BadPickup):
        listening.clean_pickup(None)


# --- storage -----------------------------------------------------------------


def test_set_replaces_and_clear_removes(db):
    assert listening.get_pickup(db, "cove") is None
    listening.set_pickup(db, "cove", "within the hour")
    listening.set_pickup(db, "cove", "once a day")
    assert listening.get_pickup(db, "cove") == "once a day"
    assert listening.clear_pickup(db, "cove") is True
    assert listening.get_pickup(db, "cove") is None
    assert listening.clear_pickup(db, "cove") is False


def test_a_refused_line_leaves_the_old_one(db):
    listening.set_pickup(db, "cove", "within the hour")
    with pytest.raises(listening.BadPickup):
        listening.set_pickup(db, "cove", "a\nb")
    assert listening.get_pickup(db, "cove") == "within the hour"


# --- rendering ---------------------------------------------------------------


def test_the_line_says_who_says_it_once_delivered_to_slack(db, bridge):
    _, sent = _ask(db)
    _seen(db, "cove")
    _deliver(db, sent.id)
    listening.set_pickup(db, "cove", "within the hour")
    a = listening.line(listening.for_decision(db, "cove", sent.id))
    assert a.startswith(
        "No session is waiting. Your answer is delivered to cove in Slack. "
    )
    assert "cove says: within the hour." in a
    assert "next check" not in a
    assert "not in Slack" not in a


def test_not_sent_state_carries_it_without_a_bridge(db):
    _, asked = _ask(db)
    _seen(db, "cove")
    listening.set_pickup(db, "cove", "within the hour")
    line = listening.line(listening.for_decision(db, "cove", asked.id))
    assert "not in Slack" in line and "cove says: within the hour." in line
    assert "reads it when it next looks" not in line


def test_a_line_that_ends_its_own_sentence_gets_no_extra_stop(db):
    _, asked = _ask(db, agent="other", session="other/1")
    _seen(db, "other", "other/1")
    listening.set_pickup(db, "other", "by tomorrow!")
    line = listening.line(listening.for_decision(db, "other", asked.id))
    assert "other says: by tomorrow!" in line and "by tomorrow!." not in line


def test_the_other_states_ignore_it(db, bridge):
    from test_listening import _hold

    _, delayed = _ask(db)
    listening.set_pickup(db, "cove", "within the hour")
    assert "says" not in listening.line(listening.for_decision(db, "cove", delayed.id))
    _hold(db, "cove", "cove/1")
    assert "says" not in listening.line(listening.for_decision(db, "cove", delayed.id))


def test_without_a_line_the_wording_is_unchanged(db, bridge):
    _, asked = _ask(db)
    _deliver(db, asked.id)
    assert "acted on at its next check" in listening.line(
        listening.for_decision(db, "cove", asked.id)
    )


def test_the_page_shows_it_escaped(db, client, bridge):
    page, asked = _ask(db)
    _seen(db)
    _deliver(db, asked.id)
    listening.set_pickup(db, "cove", '<script>alert(1)</script> & "x"')
    html = TestClient(create_gallery_app(db)).get(f"/a/{page.id}").text
    assert "<script>alert(1)</script>" not in html
    assert "cove says: &lt;script&gt;alert(1)&lt;/script&gt; &amp;" in html
    assert "acted on at its next check" not in html


def test_one_agents_line_never_shows_on_anothers_question(db, bridge):
    _, asked = _ask(db, agent="other")
    _deliver(db, asked.id)
    listening.set_pickup(db, "cove", "within the hour")
    assert "within the hour" not in listening.line(
        listening.for_decision(db, "other", asked.id)
    )


# --- the line lapses ----------------------------------------------------------


def _route(db):
    _, asked = _ask(db)
    listening.set_pickup(db, "cove", "within the hour")
    return asked


def test_a_fresh_reader_shows_the_line(db):
    asked = _route(db)
    _seen(db, ago=timedelta(hours=23))
    route = listening.for_decision(db, "cove", asked.id)
    assert route.pickup == "within the hour" and not route.pickup_stale
    assert "cove says: within the hour." in listening.line(route)


def test_a_reader_last_seen_25_hours_ago_gets_the_plain_wording(db):
    asked = _route(db)
    _seen(db, ago=timedelta(hours=25))
    route = listening.for_decision(db, "cove", asked.id)
    assert route.pickup is None and route.pickup_stale
    text = listening.line(route)
    assert "says" not in text and "Last seen 1 d ago" in text
    assert listening.get_pickup(db, "cove") == "within the hour", "not deleted"
    view = listening.route_view(route)
    assert view["pickup"] is None and view["pickup_stale"] is True


def test_never_seen_gets_the_plain_wording(db):
    asked = _route(db)
    route = listening.for_decision(db, "cove", asked.id)
    assert route.pickup is None and route.pickup_stale
    assert "says" not in listening.line(route)
    assert "has not been seen" in listening.line(route)


def test_a_fresh_check_brings_the_line_back(db):
    asked = _route(db)
    _seen(db, ago=timedelta(hours=30))
    assert listening.for_decision(db, "cove", asked.id).pickup is None
    events.note_read(db, "cove", "cove/1")
    route = listening.for_decision(db, "cove", asked.id)
    assert route.pickup == "within the hour" and not route.pickup_stale


def test_no_stored_line_is_not_stale(db):
    _, asked = _ask(db)
    route = listening.for_decision(db, "cove", asked.id)
    assert route.pickup is None and not route.pickup_stale


def test_the_body_states_the_rule_and_the_stored_line(db, client):
    token, _ = keys.mint(db, agent="cove")
    asked = _route(db)
    _seen(db, ago=timedelta(hours=26))
    body = client.get(
        f"/api/v1/listening?decision={asked.id}", headers=auth(token)
    ).json()
    assert body["pickup"] == "within the hour"
    assert body["pickup_fresh_hours"] == 24
    assert body["decision"]["pickup"] is None
    assert body["decision"]["pickup_stale"] is True


# --- the API ------------------------------------------------------------------


def test_put_sets_get_shows_delete_clears(db, client):
    token, _ = keys.mint(db, agent="cove")
    r = _put(client, token, "  within the hour ")
    assert r.status_code == 200 and r.json() == {"pickup": "within the hour"}
    assert client.get("/api/v1/listening", headers=auth(token)).json()["pickup"] == (
        "within the hour"
    )
    d = client.delete("/api/v1/listening/pickup", headers=auth(token))
    assert d.json() == {"pickup": None, "cleared": True}
    assert client.get("/api/v1/listening", headers=auth(token)).json()["pickup"] is None
    again = client.delete("/api/v1/listening/pickup", headers=auth(token))
    assert again.json()["cleared"] is False


def test_the_decision_in_the_body_carries_the_pickup_and_its_line(db, client, bridge):
    _, asked = _ask(db)
    _seen(db, "cove")
    _deliver(db, asked.id)
    token, _ = keys.mint(db, agent="cove")
    _put(client, token, "within the hour")
    body = client.get(
        f"/api/v1/listening?decision={asked.id}", headers=auth(token)
    ).json()
    assert body["decision"]["pickup"] == "within the hour"
    assert "cove says: within the hour." in body["decision"]["line"]


@pytest.mark.parametrize("text", ["a\nb", "", "x" * 121, "\u200b"])
def test_put_refuses_a_bad_line_with_422_and_changes_nothing(db, client, text):
    token, _ = keys.mint(db, agent="cove")
    _put(client, token, "kept")
    assert _put(client, token, text).status_code == 422
    assert listening.get_pickup(db, "cove") == "kept"


def test_put_refuses_an_all_filler_line_like_an_empty_one(db, client):
    token, _ = keys.mint(db, agent="cove")
    _put(client, token, "kept")
    for text in ("", "\u3164", "\u3164 \u200b"):
        assert _put(client, token, text).status_code == 422
    assert listening.get_pickup(db, "cove") == "kept"


def test_put_refuses_a_missing_or_non_text_pickup(db, client):
    token, _ = keys.mint(db, agent="cove")
    for body in ({}, {"pickup": None}, {"pickup": 5}):
        r = client.put("/api/v1/listening/pickup", json=body, headers=auth(token))
        assert r.status_code == 422


def test_each_key_sets_only_its_own_agents_line(db, client):
    cove, _ = keys.mint(db, agent="cove")
    other, _ = keys.mint(db, agent="other")
    _put(client, cove, "within the hour")
    assert listening.get_pickup(db, "other") is None
    client.delete("/api/v1/listening/pickup", headers=auth(other))
    assert listening.get_pickup(db, "cove") == "within the hour"
    body = client.get("/api/v1/listening", headers=auth(other)).json()
    assert body["pickup"] is None


def test_the_routes_need_a_key(client):
    assert (
        client.put("/api/v1/listening/pickup", json={"pickup": "x"}).status_code == 401
    )
    assert client.delete("/api/v1/listening/pickup").status_code == 401


# --- rename ---------------------------------------------------------------------


def test_a_rename_moves_the_line(db):
    listening.set_pickup(db, "old-name", "within the hour")
    rename.rename_agent(db, "old-name", "new-name", dry_run=False)
    assert listening.get_pickup(db, "new-name") == "within the hour"
    assert listening.get_pickup(db, "old-name") is None


def test_a_rename_into_an_agent_with_a_line_keeps_the_newer(db):
    listening.set_pickup(db, "new-name", "older")
    listening.set_pickup(db, "old-name", "newer")
    rename.rename_agent(db, "old-name", "new-name", dry_run=False)
    assert listening.get_pickup(db, "new-name") == "newer"
    assert listening.get_pickup(db, "old-name") is None


def test_a_rename_keeps_the_new_names_line_when_it_is_newer(db):
    listening.set_pickup(db, "old-name", "older")
    listening.set_pickup(db, "new-name", "newer")
    rename.rename_agent(db, "old-name", "new-name", dry_run=False)
    assert listening.get_pickup(db, "new-name") == "newer"


# --- the CLI ----------------------------------------------------------------------


def test_cli_sets_shows_and_clears(db, live, key_file, capsys, monkeypatch):
    path, _ = key_file
    assert _run(["pickup"], path, monkeypatch) == 0
    assert "no pickup line set" in capsys.readouterr().out
    assert _run(["pickup", "within the hour"], path, monkeypatch) == 0
    assert "pickup set: within the hour" in capsys.readouterr().out
    assert _run(["pickup"], path, monkeypatch) == 0
    assert capsys.readouterr().out.strip() == "within the hour"
    assert _run(["pickup", "--clear"], path, monkeypatch) == 0
    assert "pickup cleared" in capsys.readouterr().out
    assert _run(["pickup"], path, monkeypatch) == 0
    assert "no pickup line set" in capsys.readouterr().out


def test_cli_reports_a_refused_line_and_exits_1(
    db, live, key_file, capsys, monkeypatch
):
    path, _ = key_file
    assert _run(["pickup", "x" * 200], path, monkeypatch) == 1
    assert "at most" in capsys.readouterr().err


def test_cli_refuses_a_line_with_clear(db, live, key_file, capsys, monkeypatch):
    path, _ = key_file
    assert _run(["pickup", "soon", "--clear"], path, monkeypatch) == 2


def test_cli_help_says_it_is_the_agents_word(capsys):
    from artifacts import decide

    with pytest.raises(SystemExit):
        decide.main(["pickup", "--help"])
    out = capsys.readouterr().out
    assert "your word" in out and "--clear" in out


def test_cli_takes_a_line_that_starts_with_a_dash(
    db, live, key_file, capsys, monkeypatch
):
    path, _ = key_file
    for argv in (
        ["pickup", "--", "- reading it now"],
        ["pickup", "- reading it now"],
        ["pickup", "-", "reading", "now"],
    ):
        assert _run(argv, path, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "pickup set: - reading it now" in out and "pickup set: - reading now" in out


def test_cli_help_says_how_to_pass_a_dash_line(capsys):
    from artifacts import decide

    with pytest.raises(SystemExit):
        decide.main(["pickup", "--help"])
    assert "starts with" in capsys.readouterr().out


def test_onboarding_names_the_command_and_that_it_is_unchecked():
    text = onboarding.agents_txt("https://artifacts.invalid")
    assert 'artifacts-decide pickup "within the hour"' in text
    assert "/api/v1/listening/pickup" in text
    assert "never checked" in text
