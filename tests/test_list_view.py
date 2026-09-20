"""Grid or list, and the list grouped by date.

homelab-ij8.84.4. Jeremy called this out specifically: "I hope you notice you
can toggle between a list view and a thumbnail view." The shape is measured off
the saved claude.ai list page: a 12px/500 muted date heading, a 40px row, and a
title at weight 400 -- lighter than the card title, because in a list the title
is the only thing and has nothing to win against.

The date grouping is the part with a real failure mode in it, so most of what is
asserted here is about time rather than markup.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from artifacts import store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Europe/Paris")
    # https, not the default http: the view cookie is set `secure`, so over a
    # plain-http test client the browser-equivalent would refuse to store it and
    # the "it is remembered" test would fail against correct production
    # behaviour. Testing the real configuration rather than weakening it.
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


def _age(db, artifact_id: str, when: datetime) -> None:
    with db.connect() as conn:
        conn.execute(
            "update artifact set updated_at = %s where id = %s", (when, artifact_id)
        )


def _headings(page: str) -> list[str]:
    return re.findall(r'<h2 class="daygroup">([^<]*)</h2>', page)


# --- choosing a view ---------------------------------------------------------


def test_the_default_is_the_grid_and_the_parameter_switches_it(client, db):
    store.publish(db, "seat", "Board", PAGE)
    assert '<ul class="tiles">' in client.get("/").text

    listed = client.get("/?view=list").text
    assert '<ul class="rows">' in listed
    assert '<ul class="tiles">' not in listed


def test_the_toggle_offers_the_view_you_are_not_in(client, db):
    """An icon showing the state you are already in reads as a label and gets
    ignored. It has to offer the move."""
    store.publish(db, "seat", "Board", PAGE)
    assert 'href="/?view=list"' in client.get("/").text
    assert 'href="/?view=grid"' in client.get("/?view=list").text


def test_the_choice_is_remembered_but_only_when_it_was_made(client, db):
    """Writing the cookie on every load would let a shared ?view=list link
    change what the recipient sees everywhere afterwards, rather than just on
    the page they opened."""
    store.publish(db, "seat", "Board", PAGE)

    plain = client.get("/")
    assert "artifacts_view" not in plain.headers.get("set-cookie", "")

    chosen = client.get("/?view=list")
    assert "artifacts_view=list" in chosen.headers["set-cookie"]
    assert '<ul class="rows">' in client.get("/").text, "and it survives the next load"


def test_the_parameter_beats_the_remembered_choice(client, db):
    """So a link says what it shows."""
    store.publish(db, "seat", "Board", PAGE)
    client.get("/?view=list")
    assert '<ul class="tiles">' in client.get("/?view=grid").text


def test_a_nonsense_view_falls_back_rather_than_failing(client, db):
    store.publish(db, "seat", "Board", PAGE)
    got = client.get("/?view=../../etc/passwd")
    assert got.status_code == 200
    assert '<ul class="tiles">' in got.text


# --- the grouping ------------------------------------------------------------


def test_pages_are_grouped_under_the_day_they_last_moved(client, db):
    now = datetime.now(UTC)
    today = store.publish(db, "seat", "Today's", PAGE)
    yesterday = store.publish(db, "seat", "Yesterday's", PAGE)
    older = store.publish(db, "seat", "Older", PAGE)
    _age(db, today.id, now)
    _age(db, yesterday.id, now - timedelta(days=1))
    _age(db, older.id, now - timedelta(days=9))

    headings = _headings(client.get("/?view=list").text)
    assert headings[:2] == ["Today", "Yesterday"]
    assert len(headings) == 3, "and the old one under its own day"
    assert headings[2] not in ("Today", "Yesterday")


def test_one_heading_per_day_not_one_per_page(client, db):
    now = datetime.now(UTC)
    for n in range(3):
        published = store.publish(db, "seat", f"Page {n}", PAGE)
        _age(db, published.id, now - timedelta(minutes=n))
    assert _headings(client.get("/?view=list").text) == ["Today"]


def test_the_day_is_his_day_not_utc(client, db, monkeypatch):
    """A page published at 00:40 in Paris is 22:40 the previous day in UTC.
    Grouping on the raw timestamp would file this morning's work under
    yesterday -- the same class of error as the 08:53 UTC stamp he caught on a
    board, and harder to spot, because a heading looks authoritative."""
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Europe/Paris")
    from zoneinfo import ZoneInfo

    paris = ZoneInfo("Europe/Paris")
    just_after_midnight = datetime.now(paris).replace(
        hour=0, minute=40, second=0, microsecond=0
    )
    if just_after_midnight > datetime.now(paris):
        pytest.skip("it is before 00:40 locally; the case cannot be staged today")

    published = store.publish(db, "seat", "Early", PAGE)
    _age(db, published.id, just_after_midnight.astimezone(UTC))
    assert _headings(client.get("/?view=list").text) == ["Today"]


# --- the row -----------------------------------------------------------------


def test_a_row_carries_exactly_the_same_actions_as_a_tile(client, db):
    """Parity, asserted as parity rather than as a list of strings. The two
    views share `_tile_menu`, and the point of that is that switching view
    never quietly costs you an action -- so the test compares the two menus
    instead of restating what is in them, and cannot drift when the menu does.
    """
    published = store.publish(db, "seat", "Board", PAGE)

    def actions(html: str) -> set[str]:
        panel = html.split('<div class="mpanel right">', 1)[1]
        panel = panel.split("</details>", 1)[0]
        return set(re.findall(r'(?:action|href)="([^"]+)"', panel))

    assert actions(client.get("/").text) == actions(client.get("/?view=list").text)
    assert f"/a/{published.id}/pin" in actions(client.get("/?view=list").text)


def test_a_row_for_a_page_with_a_question_says_so_and_takes_the_anchor(client, db):
    from artifacts import decisions

    published = store.publish(db, "seat", "Board", PAGE)
    decisions.ask(
        db,
        agent="seat",
        artifact_id=published.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    page = client.get("/?view=list").text
    assert "1 question for you" in page
    assert 'class="row-wrap asking"' in page


# --- archived ----------------------------------------------------------------


def test_archived_pages_are_counted_folded_and_visibly_secondary(client, db):
    """Out of the way must never become lost, so it is counted; and a page off
    the wall must not compete with one on it, so its contents are recessed."""
    live = store.publish(db, "seat", "Live", PAGE)
    gone = store.publish(db, "seat", "Archived", PAGE)
    store.set_archived(db, gone.id, True, agent=None)

    page = client.get("/").text
    assert "<span>Archived</span>" in page
    assert '<span class="n">1 page</span>' in page
    assert '<div class="archived-body">' in page
    body = page.split('<div class="archived-body">', 1)[1]
    assert gone.id in body
    assert live.id not in body, "a live page is never in the archived fold"


def test_the_archived_fold_works_in_either_view(client, db):
    gone = store.publish(db, "seat", "Archived", PAGE)
    store.set_archived(db, gone.id, True, agent=None)
    grid = client.get("/").text
    listed = client.get("/?view=list").text
    assert '<ul class="tiles">' in grid.split('<div class="archived-body">', 1)[1]
    assert '<ul class="rows">' in listed.split('<div class="archived-body">', 1)[1]


def test_no_archived_pages_means_no_fold_at_all(client, db):
    store.publish(db, "seat", "Live", PAGE)
    # The ELEMENT, not the string: "archived-list" on its own also matches the
    # stylesheet, which is in the page whether or not anything is archived.
    assert '<details class="archived-list">' not in client.get("/").text
