"""`GET /api/v1/pages/{id}/stats`: an agent reads how often ITS OWN page was read
(runcove-5t3oj.158.1, slice 3). Both words, both databases (the `db` fixture); another
agent's page answers exactly as a page that is not there."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import email_share, keys, pageviews, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body>one</body></html>"
MISSING = "abcdefghijklmnopqrstuvwxyz"


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def seat(db):
    token, _ = keys.mint(db, agent="seat")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other(db):
    token, _ = keys.mint(db, agent="other")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def page(db, seat):
    return store.publish(db, "seat", "Board", PAGE).id


def get(api, page_id, headers, word="pages", **params):
    return api.get(f"/api/v1/{word}/{page_id}/stats", params=params, headers=headers)


def now():
    return datetime.now(UTC)


@pytest.mark.parametrize("word", ["pages", "artifacts"])
def test_a_page_nobody_read_has_zeros_and_no_since(api, seat, page, word):
    got = get(api, page, seat, word=word, days=3)
    assert got.status_code == 200
    body = got.json()
    assert body["artifact_id"] == page
    assert body["since"] is None
    assert len(body["days"]) == 3
    assert all(
        (d["owner"], d["external"], d["agent"]) == (0, 0, 0) for d in body["days"]
    )
    assert body["totals"] == {"owner": 0, "external": 0, "agent": 0}
    assert body["shares"] == []


@pytest.mark.parametrize("word", ["pages", "artifacts"])
def test_counts_are_per_day_and_kind_with_totals_and_since(db, api, seat, page, word):
    today = now()
    pageviews.record(db, page, "agent", "reader", now=today - timedelta(days=1))
    pageviews.record(db, page, "owner", "", now=today)
    pageviews.record(db, page, "agent", "reader", now=today)
    body = get(api, page, seat, word=word, days=2).json()
    assert [d["day"] for d in body["days"]] == [
        (today - timedelta(days=1)).strftime("%Y-%m-%d"),
        today.strftime("%Y-%m-%d"),
    ]
    assert [(d["owner"], d["agent"]) for d in body["days"]] == [(0, 1), (1, 1)]
    assert body["totals"] == {"owner": 1, "external": 0, "agent": 2}
    assert body["since"] == (today - timedelta(days=1)).strftime("%Y-%m-%d")


def test_days_defaults_to_thirty_and_refuses_what_is_out_of_range(api, seat, page):
    assert len(get(api, page, seat).json()["days"]) == 30
    assert len(get(api, page, seat, days=365).json()["days"]) == 365
    for bad in (0, 366, -1, "x"):
        assert get(api, page, seat, days=bad).status_code == 422


def test_another_agents_page_answers_as_one_that_is_not_there(api, seat, other, page):
    mine = get(api, MISSING, seat)
    theirs = get(api, page, other)
    assert (theirs.status_code, theirs.json()) == (404, {"detail": "no such page"})
    assert (theirs.status_code, theirs.json()) == (mine.status_code, mine.json())


def test_the_publisher_reads_stats_of_its_own_page(db, api, seat, page):
    pageviews.record(db, page, "owner", "", now=now())
    assert get(api, page, seat).json()["totals"]["owner"] == 1


def test_it_needs_a_key(api, page):
    assert api.get(f"/api/v1/pages/{page}/stats").status_code == 401


def test_named_recipients_show_opens_and_last_open_and_the_anonymous_link_only_in_totals(
    db, api, seat, page
):
    share, _ = email_share.create(db, page, "ann@example.org")
    t = now()
    pageviews.record(db, page, "external", share.id, now=t - timedelta(hours=3))
    pageviews.record(db, page, "external", share.id, now=t)
    pageviews.record(db, page, "external", "", now=t)  # the anonymous link
    body = get(api, page, seat).json()
    assert body["totals"]["external"] == 3
    [one] = body["shares"]
    assert one["email"] == "ann@example.org"
    assert one["opens"] == 2
    assert one["withdrawn"] is False
    assert one["last_opened_at"] and one["created_at"]
    assert set(one) == {"email", "opens", "last_opened_at", "created_at", "withdrawn"}


def test_a_replaced_or_withdrawn_share_keeps_its_opens_grouped_by_email(
    db, api, seat, page
):
    first, _ = email_share.create(db, page, "ann@example.org")
    pageviews.record(db, page, "external", first.id, now=now() - timedelta(days=2))
    second, _ = email_share.create(db, page, "ann@example.org")  # replaces the first
    pageviews.record(db, page, "external", second.id, now=now())
    gone, _ = email_share.create(db, page, "bob@example.org")
    pageviews.record(db, page, "external", gone.id, now=now())
    email_share.withdraw(db, page, gone.id)
    never, _ = email_share.create(db, page, "cy@example.org")
    by = {s["email"]: s for s in get(api, page, seat).json()["shares"]}
    assert by["ann@example.org"]["opens"] == 2
    assert by["ann@example.org"]["withdrawn"] is False
    assert by["bob@example.org"]["opens"] == 1
    assert by["bob@example.org"]["withdrawn"] is True
    assert by["cy@example.org"]["opens"] == 0
    assert by["cy@example.org"]["last_opened_at"] is None
    assert never.id  # a share nobody opened is listed, with zero opens


def test_a_deleted_page_is_no_such_page(db, api, seat, page):
    store.set_deleted(db, page, True, agent="seat")
    assert get(api, page, seat).status_code == 404
