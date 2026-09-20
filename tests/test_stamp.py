"""The service fills in when a page was published, so no agent types a time.

homelab-ij8.70. Jeremy read "08:53 UTC" on a board at 10:53 on his own clock and
asked whether this could be made easier for agents. The timezone was the visible
half; the half worth fixing is that a page was carrying a time an agent had
written down.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from artifacts import keys, stamp, store
from artifacts.content import create_content_app

FRAMED = {"sec-fetch-dest": "iframe"}
MARKED = (
    "<!doctype html><html><head><title>Board</title></head>"
    f"<body><p>Updated {stamp.MARKER}</p></body></html>"
)


@pytest.fixture()
def paris(monkeypatch):
    """A fixed zone, so the assertions do not depend on where the test runs."""
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Europe/Paris")


def _content(db, artifact, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{artifact.id}.tenfourty.com"})
    return client


# --- the unit: formatting and substitution ----------------------------------


def test_the_marker_becomes_a_readable_local_time(paris):
    # 08:53 UTC is 10:53 in Paris -- the exact pair Jeremy saw and objected to.
    when = datetime(2026, 9, 20, 8, 53, tzinfo=UTC)
    out = stamp.apply(f"x {stamp.MARKER} y", when)
    assert "10:53" in out
    assert "20 September 2026" in out
    assert stamp.MARKER not in out


def test_the_zone_is_named_because_a_bare_local_time_is_ambiguous(paris):
    when = datetime(2026, 9, 20, 8, 53, tzinfo=UTC)
    assert "CEST" in stamp.format_for_display(when)


def test_a_body_with_no_marker_is_returned_untouched(paris):
    body = "<p>no marker here</p>"
    assert stamp.apply(body, datetime.now(UTC)) == body


def test_an_unknown_time_leaves_the_marker_rather_than_inventing_one():
    # Unknown is not absent. An invisible comment is a better answer than a
    # blank where a date should be, and far better than a wrong time.
    body = f"<p>{stamp.MARKER}</p>"
    assert stamp.apply(body, None) == body


def test_every_marker_in_a_page_is_filled_not_only_the_first(paris):
    when = datetime(2026, 9, 20, 8, 53, tzinfo=UTC)
    out = stamp.apply(f"{stamp.MARKER} ... {stamp.MARKER}", when)
    assert stamp.MARKER not in out
    assert out.count("10:53") == 2


def test_a_naive_time_is_read_as_utc_not_as_local(paris):
    # The database hands back a naive datetime when a column is read without its
    # zone. Guessing local there would shift every stamp by the host's offset and
    # look entirely plausible while doing it.
    naive = datetime(2026, 9, 20, 8, 53)  # noqa: DTZ001 -- naive ON PURPOSE: this is the case under test
    aware = datetime(2026, 9, 20, 8, 53, tzinfo=UTC)
    assert stamp.format_for_display(naive) == stamp.format_for_display(aware)


def test_an_unknown_zone_name_falls_back_instead_of_failing(monkeypatch):
    # A page that will not load teaches nobody anything about a misconfigured
    # variable; it just loses the page.
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Mars/Olympus_Mons")
    assert stamp.display_timezone() is None
    assert "2026" in stamp.format_for_display(datetime(2026, 9, 20, 8, 53))  # noqa: DTZ001 -- naive ON PURPOSE: this is the case under test


def test_the_zone_is_read_at_call_time_so_a_deployment_can_set_it(monkeypatch):
    # A module-level os.environ.get freezes at first import, which is how a
    # setting becomes untestable and undeployable at once.
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Europe/Paris")
    first = stamp.display_timezone()
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "UTC")
    assert stamp.display_timezone() != first


# --- served through the real content app ------------------------------------


def test_a_served_page_carries_the_time_the_service_recorded(db, monkeypatch, paris):
    keys.mint(db, agent="ops-seat")
    artifact = store.publish(db, "ops-seat", "Board", MARKED)
    token = store.read_token(db, artifact.id)
    page = _content(db, artifact, monkeypatch).get(f"/t/{token}/", headers=FRAMED)
    assert page.status_code == 200
    assert stamp.MARKER not in page.text
    assert "Updated 2" in page.text  # a day-of-month, not an empty gap


def test_the_stored_body_still_holds_the_marker_not_a_frozen_time(db):
    # Substituted when SERVED, never when stored. The stored body is hashed to
    # make `revision`, so folding a clock into it would mint a new revision on
    # every identical republish and defeat the no-op republish work outright.
    keys.mint(db, agent="ops-seat")
    artifact = store.publish(db, "ops-seat", "Board", MARKED)
    assert stamp.MARKER in store.get_body(db, "ops-seat", artifact.id).body


def test_an_identical_republish_is_not_a_new_version_at_all(db):
    """The stamp changes nothing the hash can see — the point this file exists
    to protect. Folding a clock into the STORED body would make every republish
    of an unchanged page different bytes, and the page would look freshly
    updated for ever after.

    This used to assert the identical republish produced `_revision(MARKED, 2)`
    — that it still made a version 2, just a predictable one. Since
    homelab-ij8.41 an identical body makes no version at all, which is a
    stronger form of the same guarantee and the one a scheduled republish
    depends on. If a clock ever does creep into the stored body, this fails
    loudly: the bodies would differ and a version 2 would appear.
    """
    keys.mint(db, agent="ops-seat")
    first = store.publish(db, "ops-seat", "Board", MARKED)
    second = store.patch(db, "ops-seat", first.id, MARKED, first.revision)
    assert store._revision(MARKED, 1) == first.revision
    assert second.version == first.version == 1, "no version 2 was created"
    assert second.revision == first.revision, "and the revision did not move"


def test_an_older_version_says_when_IT_was_published(db, monkeypatch, paris):
    # An inert snapshot claiming to be as fresh as the live page is the whole
    # reason the stamp comes from `version.created_at` and not `artifact.updated_at`.
    keys.mint(db, agent="ops-seat")
    artifact = store.publish(db, "ops-seat", "Board", MARKED)
    with db.connect() as conn:
        conn.execute(
            "update version set created_at = %s where artifact_id = %s and n = 1",
            (datetime(2026, 1, 2, 3, 4, tzinfo=UTC), artifact.id),
        )
    store.patch(
        db,
        "ops-seat",
        artifact.id,
        MARKED.replace("Board", "Board 2"),
        artifact.revision,
    )
    token = store.read_token(db, artifact.id)
    content = _content(db, artifact, monkeypatch)
    old = content.get(f"/t/{token}/v/1", headers=FRAMED)
    assert old.status_code == 200
    assert "2 January 2026" in old.text
    live = content.get(f"/t/{token}/", headers=FRAMED)
    assert "2 January 2026" not in live.text


def test_the_listing_says_when_each_page_last_changed(db):
    # The query has always ORDERED BY updated_at and never returned it, so every
    # client saw null and could not say when a page last moved.
    keys.mint(db, agent="ops-seat")
    before = datetime.now(UTC) - timedelta(seconds=5)
    store.publish(db, "ops-seat", "Board", MARKED)
    rows = store.list_for_agent(db, "ops-seat")
    assert len(rows) == 1
    assert rows[0].updated_at is not None
    assert rows[0].updated_at > before


def test_every_listed_field_holds_what_its_name_says(db):
    """A shifted column is the quietest bug this codebase has produced.

    `ArtifactRow(*row, ...)` took whatever the SELECT listed, in the order it
    listed it, so adding one column silently re-assigned every field after it.
    The same shape put `owning_agent` into `acknowledged_by` in decisions.py on
    17 September and every answer then tried to write an event with no agent.
    Here it surfaced as a TypeError only because the new field happened to land
    last; one position earlier it would have passed the suite and served a title
    where a URL belongs. The constructions are keyword-based now, and this
    asserts the identities rather than the count.
    """
    keys.mint(db, agent="ops-seat")
    published = store.publish(db, "ops-seat", "A Distinctive Title", MARKED)

    listed = store.list_for_agent(db, "ops-seat")[0]
    owned = store.get_owned(db, "ops-seat", published.id)
    for row in (listed, owned):
        assert row.id == published.id
        assert row.title == "A Distinctive Title"
        assert row.agent == "ops-seat"
        assert row.revision == published.revision
        assert row.url == published.url
        assert row.current_version == 1
        assert isinstance(row.updated_at, datetime)
