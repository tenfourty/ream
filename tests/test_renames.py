"""Slice 6 of the core-and-plugins design (section 6, "Renames"): neutral names for
the team/audience/partner words, with every old form still accepted and every read
carrying both. One release; the old forms go only after `news` has announced them."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ream import decide, decisions, gallery, inbox, keys, listening, sinks, store
from ream.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><p>plan</p></body></html>"
)
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db), base_url="https://testserver")


@pytest.fixture()
def seat(db):
    token, _ = keys.mint(db, agent="owner")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def page(db):
    return store.publish(db, "owner", "Plan", PAGE)


def _ask(client, seat, page, **fields):
    return client.post(
        f"/api/v1/artifacts/{page.id}/decisions",
        json={"question": "ship?", "options": OPTIONS, **fields},
        headers=seat,
    )


# --- 1. audience / for ------------------------------------------------------------


def test_audience_is_accepted_and_read_back_under_both_names(client, seat, page):
    r = _ask(client, seat, page, audience=["beta", "gamma"])
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["audience"] == ["beta", "gamma"]
    assert row["for"] == ["beta", "gamma"]
    assert "for_offices" not in row
    listed = client.get("/api/v1/questions", headers=seat).json()
    assert listed[0]["audience"] == listed[0]["for"] == ["beta", "gamma"]


def test_the_old_for_is_still_accepted_and_read_back_under_both_names(
    client, seat, page
):
    r = _ask(client, seat, page, **{"for": ["beta"]})
    assert r.status_code == 201, r.text
    assert r.json()["audience"] == ["beta"] and r.json()["for"] == ["beta"]


def test_when_both_are_sent_audience_wins(client, seat, page):
    r = _ask(client, seat, page, audience=["beta"], **{"for": ["gamma"]})
    assert r.status_code == 201, r.text
    assert r.json()["audience"] == ["beta"] and r.json()["for"] == ["beta"]


def test_neither_sent_reads_empty_under_both_names(client, seat, page):
    row = _ask(client, seat, page).json()
    assert row["audience"] == [] and row["for"] == []


@pytest.mark.parametrize("field", ["audience", "for"])
def test_the_own_team_is_refused_in_other_teams_wording(client, seat, page, field):
    r = _ask(client, seat, page, **{field: ["owner"]})
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == field
    detail = r.json()["detail"]["detail"]
    assert "other teams" in detail and "OTHER offices" not in detail


def test_audience_shape_errors_name_audience(client, seat, page):
    r = _ask(client, seat, page, audience="beta")
    assert r.status_code == 422 and r.json()["detail"]["field"] == "audience"
    r = _ask(client, seat, page, audience=["Not A Team"])
    assert r.status_code == 422 and "team names" in r.json()["detail"]["detail"]


# --- 2. CLI flags ---------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--team", "--office"])
def test_wait_and_listening_take_the_new_and_the_old_team_flag(flag):
    parser = decide.build_parser()
    assert parser.parse_args(["wait", flag, "cove"]).office == "cove"
    assert parser.parse_args(["listening", flag, "cove"]).office == "cove"


@pytest.mark.parametrize("flag", ["--all-teams", "--all-offices"])
def test_wait_takes_the_new_and_the_old_all_flag(flag):
    parser = decide.build_parser()
    args = parser.parse_args(["wait", flag])
    assert args.all_offices is True
    assert decide._office(args) is None
    assert parser.parse_args(["wait"]).all_offices is False


def test_the_old_flags_are_hidden_from_help():
    parser = decide.build_parser()
    for name in ("wait", "listening"):
        sub = next(
            a for a in parser._actions if hasattr(a, "choices") and a.choices
        ).choices[name]
        text = sub.format_help()
        assert "--team" in text and "--office" not in text
        assert "--all-offices" not in text


@pytest.mark.parametrize("flag", ["--audience", "--for"])
def test_ask_takes_audience_and_the_old_for(flag):
    parser = decide.build_parser()
    args = parser.parse_args(["ask", "PAGE", "--question", "q?", flag, "beta"])
    assert args.for_office == ["beta"]


def test_a_decision_row_prints_the_audience_under_either_name():
    base = {"id": "d1", "question": "q", "options": [], "recommended": None}
    for body in ({"audience": ["beta"]}, {"for": ["beta"]}):
        assert "also needed by beta" in decide._fmt({**base, **body})


# --- 3. inbox kind --------------------------------------------------------------


def test_the_partner_question_kind_carries_its_old_id_and_filters_accept_both():
    item = inbox.Item(
        kind="partner_question",
        id="x",
        artifact_id="",
        page_title="t",
        office="o",
        created_at=None,
    )
    assert item.view() == {"kind": "partner_question", "legacy_kind": "slack_question"}
    assert inbox.kind_matches(item, "partner_question")
    assert inbox.kind_matches(item, "slack_question")
    assert not inbox.kind_matches(item, "question")
    plain = inbox.Item(
        kind="question",
        id="x",
        artifact_id="",
        page_title="t",
        office="o",
        created_at=None,
    )
    assert plain.view() == {"kind": "question"}


# --- 4. listening states ---------------------------------------------------------


def _route(state):
    return listening.Route(
        id="d1",
        agent="cove",
        state=state,
        waiting_session=None,
        last_seen=None,
        announced=True,
        delivered=state == listening.SENT_TO_PARTNER,
    )


@pytest.mark.parametrize(
    ("state", "new", "old"),
    [
        (listening.SENT_TO_PARTNER, "sent_to_partner", "sent_to_slack"),
        (listening.PARTNER_DELAYED, "partner_delayed", "slack_delayed"),
    ],
)
def test_renamed_states_carry_the_old_id_as_legacy_state(state, new, old):
    view = listening.route_view(_route(state))
    assert view["state"] == new and view["legacy_state"] == old


@pytest.mark.parametrize("state", [listening.WAITING_NOW, listening.NOT_SENT])
def test_states_that_kept_their_name_carry_no_legacy_state(state):
    assert "legacy_state" not in listening.route_view(_route(state))


# --- 5. labels ---------------------------------------------------------------------


@pytest.fixture()
def registry(monkeypatch):
    monkeypatch.setattr(decisions, "_REGISTERED", list(decisions._REGISTERED))
    monkeypatch.setattr(decisions, "_LABELS", dict(decisions._LABELS))


def test_slack_is_registered_in_core_with_its_label():
    assert decisions.is_known_source("slack")
    assert decisions.source_label("slack") == "Slack"


def test_a_source_with_a_label_shows_it_and_one_without_shows_its_value(registry):
    decisions.register_source("matrix", label="Matrix")
    decisions.register_source("plain")
    assert decisions.source_label("matrix") == "Matrix"
    assert decisions.source_label("plain") == "plain"
    assert decisions.source_label("never-registered") == "never-registered"
    assert decisions.is_known_source("matrix") and decisions.is_known_source("plain")


def test_a_bad_label_is_refused(registry):
    for bad in ("", "  ", "x" * 41):
        with pytest.raises(ValueError):
            decisions.register_source("matrix", label=bad)


def test_the_wording_follows_the_label(registry):
    copy = SimpleNamespace(link="https://chat.invalid/x")
    assert "Open in Slack</a>" in gallery._slack_link(copy)
    assert gallery._slack_link(SimpleNamespace(link="")) == "Asked in Slack"
    assert "in Slack," in listening.line(_route(listening.SENT_TO_PARTNER))
    decisions._LABELS["slack"] = "Chat"
    assert "Open in Chat</a>" in gallery._slack_link(copy)
    assert gallery._slack_link(SimpleNamespace(link="")) == "Asked in Chat"
    assert "in Chat," in listening.line(_route(listening.SENT_TO_PARTNER))
    assert "Slack" not in listening.line(_route(listening.PARTNER_DELAYED))


def test_a_sink_label_names_it_in_wording_and_an_unlabelled_one_its_name():
    assert sinks.label_of("partner") == "quarterdeck"
    assert sinks.label_of("nudge") == "nudge"
    assert sinks.label_of("not-a-sink") == "not-a-sink"
    assert gallery._sink_name() == "quarterdeck"
