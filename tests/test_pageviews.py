"""How often a page is read (runcove-5t3oj.158.1, slice 1: storage and recorder, not yet
wired into any request path). The windows, the per-day counts, the cascade, the rule that
recording never fails the caller, and a rename moving an agent's own reads."""

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ream import pageviews, rename, store

PAGE = "<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>"
T0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def page(db):
    return store.publish(db, "publisher", "Plan", PAGE).id


def _viewer(db, page_id, kind, viewer):
    with db.connect() as conn:
        return conn.execute(
            "select views, last_at, counted_at from page_viewer"
            " where artifact_id = %s and kind = %s and viewer = %s",
            (page_id, kind, viewer),
        ).fetchone()


def _day(db, page_id):
    with db.connect() as conn:
        return {
            (day, kind): views
            for day, kind, views in conn.execute(
                "select day, kind, views from page_view_day where artifact_id = %s",
                (page_id,),
            ).fetchall()
        }


def test_a_first_view_counts(db, page):
    assert pageviews.record(db, page, "owner", "", now=T0) is True
    views, last_at, counted_at = _viewer(db, page, "owner", "")
    assert (views, last_at, counted_at) == (1, T0, T0)
    assert _day(db, page) == {("2026-11-22", "owner"): 1}


def test_a_repeat_inside_the_window_does_not_count_but_moves_last_at(db, page):
    pageviews.record(db, page, "agent", "reader", now=T0)
    later = T0 + timedelta(minutes=29)
    assert pageviews.record(db, page, "agent", "reader", now=later) is False
    views, last_at, counted_at = _viewer(db, page, "agent", "reader")
    assert (views, last_at, counted_at) == (1, later, T0)
    assert _day(db, page) == {("2026-11-22", "agent"): 1}


def test_a_view_after_the_window_counts_again(db, page):
    pageviews.record(db, page, "agent", "reader", now=T0)
    pageviews.record(db, page, "agent", "reader", now=T0 + timedelta(minutes=29))
    # the window runs from the last COUNTED view, not from the last repeat
    again = T0 + timedelta(minutes=31)
    assert pageviews.record(db, page, "agent", "reader", now=again) is True
    views, last_at, counted_at = _viewer(db, page, "agent", "reader")
    assert (views, last_at, counted_at) == (2, again, again)
    assert _day(db, page) == {("2026-11-22", "agent"): 2}


def test_the_anonymous_link_has_a_five_minute_window(db, page):
    assert pageviews.record(db, page, "external", "", now=T0) is True
    assert (
        pageviews.record(db, page, "external", "", now=T0 + timedelta(minutes=4))
        is False
    )
    assert (
        pageviews.record(db, page, "external", "", now=T0 + timedelta(minutes=6))
        is True
    )
    assert _viewer(db, page, "external", "")[0] == 2


def test_a_named_recipient_keeps_the_thirty_minute_window(db, page):
    pageviews.record(db, page, "external", "share1", now=T0)
    assert (
        pageviews.record(db, page, "external", "share1", now=T0 + timedelta(minutes=6))
        is False
    )


def test_viewers_and_kinds_are_counted_apart(db, page):
    pageviews.record(db, page, "agent", "one", now=T0)
    assert pageviews.record(db, page, "agent", "two", now=T0) is True
    assert pageviews.record(db, page, "owner", "", now=T0) is True
    assert pageviews.record(db, page, "external", "", now=T0) is True
    assert _day(db, page) == {
        ("2026-11-22", "agent"): 2,
        ("2026-11-22", "owner"): 1,
        ("2026-11-22", "external"): 1,
    }


def test_the_day_rolls_over_at_utc_midnight(db, page):
    before = datetime(2026, 11, 22, 23, 50, tzinfo=UTC)
    after = datetime(2026, 11, 23, 0, 10, tzinfo=UTC)
    pageviews.record(db, page, "owner", "", now=before)
    assert pageviews.record(db, page, "owner", "", now=after) is False  # same window
    assert pageviews.record(db, page, "owner", "", now=after + timedelta(hours=1))
    assert _day(db, page) == {("2026-11-22", "owner"): 1, ("2026-11-23", "owner"): 1}


def test_a_time_in_another_zone_is_counted_on_its_utc_day(db, page):
    plus_two = datetime(2026, 11, 23, 1, 0, tzinfo=UTC).astimezone(
        timezone(timedelta(hours=2))
    )
    pageviews.record(db, page, "owner", "", now=plus_two)
    assert _day(db, page) == {("2026-11-23", "owner"): 1}


@pytest.mark.parametrize(
    ("kind", "viewer"),
    [
        ("admin", ""),
        ("", ""),
        ("Owner", ""),
        ("owner", "jeremy"),  # the owner's views are stored under '', never his id
        ("agent", ""),  # an agent has a name
    ],
)
def test_an_invalid_kind_or_viewer_is_refused_and_stores_nothing(
    db, page, kind, viewer
):
    assert pageviews.record(db, page, kind, viewer, now=T0) is False
    assert _day(db, page) == {}
    with db.connect() as conn:
        assert conn.execute("select count(*) from page_viewer").fetchone()[0] == 0


def test_a_page_that_does_not_exist_stores_nothing_and_does_not_raise(db):
    assert pageviews.record(db, "a" * 26, "owner", "", now=T0) is False
    with db.connect() as conn:
        assert conn.execute("select count(*) from page_viewer").fetchone()[0] == 0
        assert conn.execute("select count(*) from page_view_day").fetchone()[0] == 0


def test_deleting_a_page_deletes_its_counts(db, page):
    other = store.publish(db, "publisher", "Other", PAGE).id
    pageviews.record(db, page, "owner", "", now=T0)
    pageviews.record(db, other, "owner", "", now=T0)
    with db.connect() as conn:
        conn.execute("delete from artifact where id = %s", (page,))
    assert _viewer(db, page, "owner", "") is None
    assert _day(db, page) == {}
    assert _viewer(db, other, "owner", "") is not None
    assert _day(db, other) == {("2026-11-22", "owner"): 1}


def test_record_never_raises_on_a_broken_database(caplog):
    class Broken:
        def connect(self):
            raise RuntimeError("secret detail the log must not repeat")

    with caplog.at_level("DEBUG", logger="ream.pageviews"):
        assert pageviews.record(Broken(), "a" * 26, "owner", "", now=T0) is False
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text


def test_record_emits_no_event(db, page):
    with db.connect() as conn:
        before = conn.execute("select count(*) from event").fetchone()[0]
    pageviews.record(db, page, "agent", "reader", now=T0)
    pageviews.record(db, page, "external", "", now=T0)
    with db.connect() as conn:
        assert conn.execute("select count(*) from event").fetchone()[0] == before


def test_daily_is_zero_filled_oldest_first_and_ends_today(db, page):
    pageviews.record(db, page, "owner", "", now=T0)
    pageviews.record(db, page, "agent", "reader", now=T0 - timedelta(days=2))
    pageviews.record(db, page, "agent", "reader", now=T0 - timedelta(days=40))
    rows = pageviews.daily(db, page, days=4, now=T0)
    assert rows == [
        {"day": "2026-11-19", "owner": 0, "external": 0, "agent": 0},
        {"day": "2026-11-20", "owner": 0, "external": 0, "agent": 1},
        {"day": "2026-11-21", "owner": 0, "external": 0, "agent": 0},
        {"day": "2026-11-22", "owner": 1, "external": 0, "agent": 0},
    ]
    assert len(pageviews.daily(db, page, days=30, now=T0)) == 30
    assert len(pageviews.daily(db, "b" * 26, days=7, now=T0)) == 7


def test_viewers_lists_one_page_most_recent_first(db, page):
    other = store.publish(db, "publisher", "Other", PAGE).id
    pageviews.record(db, page, "agent", "early", now=T0)
    pageviews.record(db, page, "external", "share1", now=T0 + timedelta(hours=1))
    pageviews.record(db, other, "owner", "", now=T0 + timedelta(hours=2))
    found = pageviews.viewers(db, page)
    assert [(v.kind, v.viewer, v.views) for v in found] == [
        ("external", "share1", 1),
        ("agent", "early", 1),
    ]
    assert found[0].last_at == T0 + timedelta(hours=1)


def test_a_rename_moves_the_agents_own_reads(db):
    first = store.publish(db, "publisher", "One", PAGE).id
    second = store.publish(db, "publisher", "Two", PAGE).id
    third = store.publish(db, "publisher", "Three", PAGE).id
    pageviews.record(db, first, "agent", "old-reader", now=T0)
    pageviews.record(db, second, "agent", "old-reader", now=T0)
    pageviews.record(db, second, "agent", "new-reader", now=T0 + timedelta(hours=2))
    pageviews.record(db, second, "agent", "new-reader", now=T0 + timedelta(hours=3))
    pageviews.record(db, third, "agent", "someone-else", now=T0)
    pageviews.record(db, third, "owner", "", now=T0)

    counts = rename.rename_agent(db, "old-reader", "new-reader", dry_run=False)

    assert counts["page_viewer.viewer"] == 2
    assert _viewer(db, first, "agent", "old-reader") is None
    assert _viewer(db, second, "agent", "old-reader") is None
    # moved
    assert _viewer(db, first, "agent", "new-reader") == (1, T0, T0)
    # merged: the counts add, the later times win
    late = T0 + timedelta(hours=3)
    assert _viewer(db, second, "agent", "new-reader") == (3, late, late)
    # untouched
    assert _viewer(db, third, "agent", "someone-else") == (1, T0, T0)
    assert _viewer(db, third, "owner", "") == (1, T0, T0)
    # the per-day counts hold no names and are unchanged
    assert _day(db, second) == {("2026-11-22", "agent"): 3}


def test_a_dry_run_rename_leaves_the_reads_where_they_are(db, page):
    pageviews.record(db, page, "agent", "old-reader", now=T0)
    counts = rename.rename_agent(db, "old-reader", "new-reader", dry_run=True)
    assert counts["page_viewer.viewer"] == 1
    assert _viewer(db, page, "agent", "old-reader") is not None
    assert _viewer(db, page, "agent", "new-reader") is None


@pytest.mark.usefixtures("fresh_schema")
def test_views_since_is_written_once_at_migration(db):
    db.reset()
    db.migrate()  # SQLite's reset only drops
    with db.connect() as conn:
        (value,) = conn.execute(
            "select value from service_meta where key = 'views_since'"
        ).fetchone()
        assert re.fullmatch(r"\d{4}-\d\d-\d\d", value)
        conn.execute(
            "update service_meta set value = '2026-01-01' where key = 'views_since'"
        )
    db.migrate()  # a later start leaves it alone
    with db.connect() as conn:
        assert conn.execute(
            "select value from service_meta where key = 'views_since'"
        ).fetchone() == ("2026-01-01",)
