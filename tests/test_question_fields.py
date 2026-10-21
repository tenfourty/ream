"""What a question carries (runcove-5t3oj.39.1): context, topic, depends_on,
replaces, urgency with blocked_on / needed_by, and the offices that also need
the answer. The shapes are a contract with quarterdeck, so the JSON names are
asserted exactly."""

import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from artifacts import db as dbmod
from artifacts import decide, decisions, keys, migrate, onboarding, store
from artifacts.app import create_gallery_app
from artifacts.db import Database

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><p>plan</p></body></html>"
)
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]
NEW_KEYS = {
    "context",
    "topic",
    "depends_on",
    "replaces",
    "replaced_by",
    "urgency",
    "blocked_on",
    "needed_by",
    "for",
}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db), base_url="https://testserver")


@pytest.fixture()
def seats(db):
    owner, _ = keys.mint(db, agent="owner")
    other, _ = keys.mint(db, agent="other")
    return {
        "owner": {"Authorization": f"Bearer {owner}"},
        "other": {"Authorization": f"Bearer {other}"},
    }


@pytest.fixture()
def page(db):
    return store.publish(db, "owner", "Plan", PAGE)


@pytest.fixture()
def other_page(db):
    return store.publish(db, "other", "Theirs", PAGE)


def ask(client, seats, page, who="owner", **fields):
    return client.post(
        f"/api/v1/artifacts/{page.id}/decisions",
        json={"question": "ship?", "options": OPTIONS, **fields},
        headers=seats[who],
    )


def ok(client, seats, page, who="owner", **fields):
    response = ask(client, seats, page, who, **fields)
    assert response.status_code == 201, response.text
    return response.json()


def refused(client, seats, page, field, who="owner", **fields):
    response = ask(client, seats, page, who, **fields)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["field"] == field
    assert response.json()["detail"]["detail"]
    return response


def test_a_plain_question_carries_the_defaults(client, seats, page):
    row = ok(client, seats, page)
    assert row["context"] == ""
    assert row["topic"] is None
    assert row["depends_on"] == []
    assert row["replaces"] is None
    assert row["replaced_by"] is None
    assert row["urgency"] == "when_you_can"
    assert row["blocked_on"] is None
    assert row["needed_by"] is None
    assert row["for"] == []
    assert "for_offices" not in row


def test_every_field_round_trips_through_create_and_both_lists(client, seats, page, db):
    first = ok(client, seats, page)
    row = ok(
        client,
        seats,
        page,
        context="the migration is half done",
        topic={"id": "infra-2026", "title": "Infrastructure"},
        depends_on=[first["id"]],
        urgency="blocking",
        blocked_on="the deploy",
        needed_by="2026-10-22T17:00:00+02:00",
        **{"for": ["beta", "gamma"]},
    )
    assert row["context"] == "the migration is half done"
    assert row["topic"] == {"id": "infra-2026", "title": "Infrastructure"}
    assert row["depends_on"] == [first["id"]]
    assert row["urgency"] == "blocking"
    assert row["blocked_on"] == "the deploy"
    assert row["for"] == ["beta", "gamma"]
    # Stored in UTC, whatever offset it arrived in.
    needed = datetime.fromisoformat(row["needed_by"])
    assert needed == datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    assert needed.utcoffset().total_seconds() == 0

    per_page = client.get(
        f"/api/v1/artifacts/{page.id}/decisions", headers=seats["owner"]
    ).json()
    mine = next(d for d in per_page if d["id"] == row["id"])
    everything = client.get("/api/v1/decisions", headers=seats["owner"]).json()
    listed = next(d for d in everything if d["id"] == row["id"])
    for read in (mine, listed):
        assert NEW_KEYS <= set(read)
        for name in NEW_KEYS:
            assert read[name] == row[name], name


def test_context_is_at_most_a_thousand_characters(client, seats, page):
    assert ok(client, seats, page, context="é" * 1000)["context"] == "é" * 1000
    refused(client, seats, page, "context", context="x" * 1001)
    refused(client, seats, page, "context", context=7)


@pytest.mark.parametrize(
    "topic",
    [
        {"id": "Upper", "title": "t"},
        {"id": "-lead", "title": "t"},
        {"id": "x" * 65, "title": "t"},
        {"id": "", "title": "t"},
        {"id": "a b", "title": "t"},
        {"id": "ok", "title": ""},
        {"id": "ok", "title": "t" * 121},
        {"id": "ok", "title": "t", "extra": 1},
        "just-a-string",
        {"title": "no id"},
    ],
)
def test_a_malformed_topic_is_refused(client, seats, page, topic):
    refused(client, seats, page, "topic", topic=topic)


def test_topic_limits_are_inclusive(client, seats, page):
    row = ok(client, seats, page, topic={"id": "a" + "-" * 63, "title": "t" * 120})
    assert row["topic"]["title"] == "t" * 120


def test_a_topic_id_used_before_may_omit_its_title_and_a_new_one_may_not(
    client, seats, page, other_page
):
    refused(client, seats, page, "topic", topic={"id": "infra"})
    ok(client, seats, page, topic={"id": "infra", "title": "Infrastructure"})
    again = ok(client, seats, page, topic={"id": "infra"})
    assert again["topic"] == {"id": "infra", "title": "Infrastructure"}
    # The latest title for the id wins.
    ok(client, seats, page, topic={"id": "infra", "title": "Infra, renamed"})
    assert ok(client, seats, page, topic={"id": "infra"})["topic"]["title"] == (
        "Infra, renamed"
    )
    # Titles are per agent: another agent's use of the slug lends nothing.
    refused(client, seats, other_page, "topic", who="other", topic={"id": "infra"})
    assert ok(client, seats, page, topic=None)["topic"] is None


def test_depends_on_takes_only_your_own_decisions(client, seats, page, other_page):
    mine = ok(client, seats, page)
    theirs = ok(client, seats, other_page, who="other")
    assert ok(client, seats, page, depends_on=[mine["id"]])["depends_on"] == [
        mine["id"]
    ]
    unknown = ask(client, seats, page, depends_on=["nope-nothing"])
    foreign = ask(client, seats, page, depends_on=[theirs["id"]])
    assert unknown.status_code == foreign.status_code == 422
    assert unknown.json()["detail"] == foreign.json()["detail"]
    assert unknown.json()["detail"]["field"] == "depends_on"


def test_depends_on_is_a_short_list_of_distinct_ids(client, seats, page):
    made = [ok(client, seats, page)["id"] for _ in range(11)]
    assert len(ok(client, seats, page, depends_on=made[:10])["depends_on"]) == 10
    refused(client, seats, page, "depends_on", depends_on=made)
    refused(client, seats, page, "depends_on", depends_on=[made[0], made[0]])
    refused(client, seats, page, "depends_on", depends_on=made[0])
    refused(client, seats, page, "depends_on", depends_on=[""])


def test_replaces_closes_the_old_question_and_names_its_replacement(
    client, seats, page, db
):
    old = ok(client, seats, page)
    new = ok(client, seats, page, replaces=old["id"])
    assert new["replaces"] == old["id"]
    stored = decisions.get_owned(db, "owner", old["id"])
    assert stored.closed_at is not None
    assert stored.acknowledged_at is not None
    assert stored.replaced_by == new["id"]
    assert stored.answer is None
    # The new one is open; the old one is only in the closed listing.
    open_ids = [
        d["id"]
        for d in client.get(
            "/api/v1/decisions?include_answered=false", headers=seats["owner"]
        ).json()
    ]
    assert new["id"] in open_ids and old["id"] not in open_ids
    closed = client.get(
        "/api/v1/decisions?include_closed=true", headers=seats["owner"]
    ).json()
    assert next(d for d in closed if d["id"] == old["id"])["replaced_by"] == new["id"]


def test_replaces_can_name_a_question_on_another_page_of_yours(client, seats, page, db):
    second = store.publish(db, "owner", "Second", PAGE)
    old = ok(client, seats, page)
    ok(client, seats, second, replaces=old["id"])
    assert decisions.get_owned(db, "owner", old["id"]).closed_at is not None


def test_replaces_unknown_and_someone_elses_answer_identically(
    client, seats, page, other_page, db
):
    theirs = ok(client, seats, other_page, who="other")
    unknown = ask(client, seats, page, replaces="nope-nothing")
    foreign = ask(client, seats, page, replaces=theirs["id"])
    assert unknown.status_code == foreign.status_code == 422
    assert unknown.json() == foreign.json()
    assert unknown.json()["detail"]["field"] == "replaces"
    # And the refused attempt closed nothing of theirs.
    assert decisions.get_owned(db, "other", theirs["id"]).closed_at is None


def test_replaces_an_answered_or_closed_question_is_a_409(client, seats, page, db):
    answered = ok(client, seats, page)
    decisions.answer(db, answered["id"], letter="a", note="", by="jeremy")
    response = ask(client, seats, page, replaces=answered["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["state"] == "answered"
    assert response.json()["detail"]["field"] == "replaces"
    assert decisions.get_owned(db, "owner", answered["id"]).replaced_by is None

    withdrawn = ok(client, seats, page)
    client.post(f"/api/v1/decisions/{withdrawn['id']}/close", headers=seats["owner"])
    response = ask(client, seats, page, replaces=withdrawn["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["state"] == "closed"

    # A question already replaced is closed, so it cannot be replaced twice.
    once = ok(client, seats, page)
    ok(client, seats, page, replaces=once["id"])
    assert ask(client, seats, page, replaces=once["id"]).status_code == 409


def test_a_refused_ask_leaves_the_replaced_question_open(client, seats, page, db):
    old = ok(client, seats, page)
    refused(client, seats, page, "urgency", replaces=old["id"], urgency="sometime")
    refused(
        client,
        seats,
        page,
        "depends_on",
        replaces=old["id"],
        depends_on=["nope-nothing"],
    )
    stored = decisions.get_owned(db, "owner", old["id"])
    assert stored.closed_at is None and stored.replaced_by is None


def test_urgency_and_its_required_companions(client, seats, page):
    refused(client, seats, page, "urgency", urgency="soon")
    refused(client, seats, page, "urgency", urgency=None)
    # blocking needs blocked_on
    refused(client, seats, page, "blocked_on", urgency="blocking")
    refused(client, seats, page, "blocked_on", urgency="blocking", blocked_on="")
    refused(client, seats, page, "blocked_on", urgency="blocking", blocked_on="x" * 201)
    row = ok(client, seats, page, urgency="blocking", blocked_on="x" * 200)
    assert row["needed_by"] is None
    # blocked_on is only for blocking
    refused(client, seats, page, "blocked_on", blocked_on="the deploy")
    refused(
        client,
        seats,
        page,
        "blocked_on",
        urgency="by",
        needed_by="2026-10-22T15:00:00Z",
        blocked_on="the deploy",
    )
    # by needs needed_by
    refused(client, seats, page, "needed_by", urgency="by")
    by = ok(client, seats, page, urgency="by", needed_by="2026-10-22T15:00:00Z")
    assert by["urgency"] == "by"
    # needed_by is optional for the other two
    both = ok(
        client,
        seats,
        page,
        urgency="blocking",
        blocked_on="the deploy",
        needed_by="2026-10-22T15:00:00Z",
    )
    assert both["needed_by"] is not None
    assert ok(client, seats, page, needed_by="2026-10-22T15:00:00Z")["urgency"] == (
        "when_you_can"
    )


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-22T15:00:00",
        "2026-10-22",
        "tomorrow",
        "",
        20261022,
        "2026-13-40T00:00:00Z",
    ],
)
def test_needed_by_must_be_a_date_time_with_a_timezone(client, seats, page, value):
    refused(client, seats, page, "needed_by", urgency="by", needed_by=value)


def test_for_names_other_offices_only(client, seats, page):
    five = ["a1", "b2", "c3", "d4", "e5"]
    assert ok(client, seats, page, **{"for": five})["for"] == five
    refused(client, seats, page, "for", **{"for": five + ["f6"]})
    refused(client, seats, page, "for", **{"for": ["beta", "beta"]})
    refused(client, seats, page, "for", **{"for": ["owner"]})
    refused(client, seats, page, "for", **{"for": ["Not An Office"]})
    refused(client, seats, page, "for", **{"for": ["-x"]})
    refused(client, seats, page, "for", **{"for": "beta"})
    assert ok(client, seats, page, **{"for": []})["for"] == []


def test_a_review_request_keeps_the_defaults(client, seats, page):
    response = client.post(
        f"/api/v1/artifacts/{page.id}/reviews", json={}, headers=seats["owner"]
    )
    assert response.status_code == 201
    row = response.json()
    assert row["kind"] == "review"
    assert row["context"] == "" and row["topic"] is None and row["for"] == []
    assert row["urgency"] == "when_you_can"


def test_the_cli_sends_only_what_was_asked_and_prints_what_is_set(monkeypatch):
    sent = {}
    monkeypatch.setattr(decide, "_api_base", lambda: "http://x")
    monkeypatch.setattr(
        decide, "_request", lambda method, url, key, body: sent.update(body) or {}
    )
    parser = decide.build_parser()
    plain = parser.parse_args(["ask", "p1", "--question", "q", "--option", "a=x"])
    decide._ask(plain, "k")
    assert set(sent) == {"question", "options", "recommended"}

    sent.clear()
    args = parser.parse_args(
        [
            "ask",
            "p1",
            "--question",
            "q",
            "--option",
            "a=x",
            "--context",
            "because",
            "--topic",
            "infra:Infra: the lot",
            "--depends-on",
            "d1",
            "--depends-on",
            "d2",
            "--replaces",
            "d0",
            "--urgency",
            "when-you-can",
            "--needed-by",
            "2026-10-22T15:00:00Z",
            "--for",
            "beta",
            "--for",
            "gamma",
        ]
    )
    decide._ask(args, "k")
    assert sent["context"] == "because"
    assert sent["topic"] == {"id": "infra", "title": "Infra: the lot"}
    assert sent["depends_on"] == ["d1", "d2"]
    assert sent["replaces"] == "d0"
    assert sent["urgency"] == "when_you_can"
    assert sent["needed_by"] == "2026-10-22T15:00:00Z"
    assert sent["for"] == ["beta", "gamma"]

    sent.clear()
    bare_topic = parser.parse_args(
        ["ask", "p1", "--question", "q", "--option", "a=x", "--topic", "infra"]
    )
    decide._ask(bare_topic, "k")
    assert sent["topic"] == {"id": "infra"}

    with pytest.raises(SystemExit):
        parser.parse_args(["ask", "p1", "--question", "q", "--urgency", "whenever"])


def test_the_cli_shows_the_fields_only_when_they_are_set():
    base = {
        "id": "d1",
        "question": "ship?",
        "options": OPTIONS,
        "recommended": "a",
        "session": "s/1",
        "context": "",
        "topic": None,
        "depends_on": [],
        "replaces": None,
        "replaced_by": None,
        "urgency": "when_you_can",
        "blocked_on": None,
        "needed_by": None,
        "for": [],
    }
    plain = decide._fmt(base)
    for word in ("topic", "urgency", "depends", "context", "replace", "needed"):
        assert word not in plain
    shown = decide._fmt(
        {
            **base,
            "topic": {"id": "infra", "title": "Infrastructure"},
            "urgency": "blocking",
            "blocked_on": "the deploy",
            "needed_by": "2026-10-22T15:00:00+00:00",
            "depends_on": ["d0"],
            "replaced_by": "d9",
            "for": ["beta"],
            "context": "because",
        }
    )
    for part in (
        "topic infra (Infrastructure)",
        "BLOCKING: the deploy",
        "needed by 2026-10-22T15:00:00+00:00",
        "depends on d0",
        "REPLACED by d9",
        "also needed by beta",
        "context: because",
    ):
        assert part in shown, part


def test_onboarding_explains_the_fields_and_says_dependents_are_held_back():
    text = onboarding.agents_txt("http://x")
    for flag in (
        "--context",
        "--topic",
        "--depends-on",
        "--replaces",
        "--urgency",
        "--blocked-on",
        "--needed-by",
        "--for",
    ):
        assert flag in text, flag
    assert "held back" in text
    assert "--depends-on" in onboarding.skill_md("http://x")


def test_the_copier_carries_and_verifies_the_new_columns(db, seats, page, tmp_path):
    old = decisions.ask(
        db, agent="owner", artifact_id=page.id, question="first?", options=OPTIONS
    )
    row = decisions.ask(
        db,
        agent="owner",
        artifact_id=page.id,
        question="second?",
        options=OPTIONS,
        context="because",
        topic={"id": "infra", "title": "Infrastructure"},
        depends_on=[old.id],
        urgency="blocking",
        blocked_on="the deploy",
        needed_by="2026-10-22T17:00:00+02:00",
        for_offices=["beta"],
    )
    third = decisions.ask(
        db,
        agent="owner",
        artifact_id=page.id,
        question="third?",
        options=OPTIONS,
        replaces=row.id,
    )
    target = Database(f"sqlite:///{tmp_path}/copy.db")
    checks = migrate.copy(db, target, log=lambda _: None)
    assert migrate.verify(db, target) == checks
    for want in (old, row, third):
        got = decisions.get_owned(target, "owner", want.id)
        assert got is not None
        assert got.context == want.context
        assert got.topic == want.topic
        assert got.depends_on == want.depends_on
        assert got.replaces == want.replaces
        assert got.urgency == want.urgency
        assert got.blocked_on == want.blocked_on
        assert datetime.fromisoformat(got.needed_by or "1970-01-01T00:00:00+00:00") == (
            datetime.fromisoformat(want.needed_by or "1970-01-01T00:00:00+00:00")
        )
        assert got.for_offices == want.for_offices
    assert decisions.get_owned(target, "owner", row.id).replaced_by == third.id
    target.close()


def test_a_sqlite_file_from_before_these_columns_is_upgraded(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    steps = dict(dbmod._SQLITE_STEPS)
    before = {n: s for n, s in steps.items() if n < 3}
    monkeypatch.setattr(dbmod, "_SQLITE_STEPS", before)
    monkeypatch.setattr(dbmod, "SQLITE_SCHEMA_VERSION", 2)
    dbmod.Database(f"sqlite:///{path}").migrate()

    def columns():
        conn = sqlite3.connect(path)
        try:
            return [r[1] for r in conn.execute("pragma table_info(decision)")]
        finally:
            conn.close()

    assert "context" not in columns()
    monkeypatch.setattr(dbmod, "_SQLITE_STEPS", steps)
    monkeypatch.setattr(dbmod, "SQLITE_SCHEMA_VERSION", max(steps))
    upgraded = dbmod.Database(f"sqlite:///{path}")
    upgraded.migrate()
    upgraded.migrate()
    wanted = {
        "context",
        "topic_id",
        "topic_title",
        "depends_on",
        "replaces",
        "replaced_by",
        "urgency",
        "blocked_on",
        "needed_by",
        "for_offices",
    }
    assert wanted <= set(columns())
    upgraded.close()


def test_ask_validates_without_the_api(db, page):
    with pytest.raises(decisions.InvalidField) as caught:
        decisions.ask(
            db,
            agent="owner",
            artifact_id=page.id,
            question="q",
            options=OPTIONS,
            urgency="by",
        )
    assert caught.value.field == "needed_by"
