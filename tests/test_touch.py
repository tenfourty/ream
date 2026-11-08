"""A page a script keeps current says so without making a version (generated pages, slice 1).

`POST /api/v1/artifacts/{id}/touch` is "checked, still current": it moves the page's checked
time and nothing the wall or a reader notices -- no version, no new `updated_at`, no unread
marker. Any publish counts as a check too, so a generator that already republishes needs no
new call. A declared interval makes a silent page lapse at 2.5 intervals; a failed check is
flagged at once. Every route is scoped to the calling key's own agent.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import keys, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p id='a'>x</p></body></html>"
OTHER = PAGE.replace(">x<", ">y<")


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


@pytest.fixture()
def mine(db):
    token, _ = keys.mint(db, agent="seat")
    return token, store.publish(db, "seat", "Board", PAGE)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def touch(client, token, page_id, **body):
    return client.post(
        f"/api/v1/artifacts/{page_id}/touch", json=body, headers=auth(token)
    )


def read(client, token, page_id):
    return client.get(f"/api/v1/artifacts/{page_id}", headers=auth(token)).json()


def age(db, page_id, **delta):
    """Make the page's last check and its last change older, as time would."""
    when = datetime.now(UTC) - timedelta(**delta)
    with db.connect() as conn:
        conn.execute(
            "update artifact set checked_at = %s where id = %s", (when, page_id)
        )
        conn.execute(
            "update version set created_at = %s where artifact_id = %s",
            (when, page_id),
        )


def versions(db, page_id):
    with db.connect() as conn:
        return conn.execute(
            "select count(*) from version where artifact_id = %s", (page_id,)
        ).fetchone()[0]


# --- a touch is not work ------------------------------------------------------


def test_a_bodyless_touch_checks_the_page_and_changes_nothing_else(client, db, mine):
    token, page = mine
    store.mark_read(db, page.id)
    before = read(client, token, page.id)
    out = touch(client, token, page.id)
    assert out.status_code == 200
    got = out.json()
    assert got["status"] == "ok"
    assert got["checked_at"] is not None
    assert got["revision"] == page.revision and got["version"] == 1
    assert got["refresh_every"] is None and got["lapses_at"] is None
    after = read(client, token, page.id)
    assert after["updated_at"] == before["updated_at"], "the wall's order is unmoved"
    assert after["seen_version"] == before["seen_version"] == 1, (
        "nothing becomes unread"
    )
    assert after["current_version"] == 1 and versions(db, page.id) == 1
    assert after["check_status"] == "ok" and after["checked_at"] == got["checked_at"]
    assert after["changed_at"] is not None


def test_a_failed_touch_is_flagged_at_once_and_carries_its_note(client, mine):
    token, page = mine
    out = touch(client, token, page.id, status="failed", note="renderer  exited 2")
    assert out.status_code == 200 and out.json()["status"] == "failed"
    row = read(client, token, page.id)
    assert row["check_status"] == "failed"
    assert row["check_note"] == "renderer exited 2", "tidied like any one-line note"
    assert row["lapsed"] is True, "failed is flagged without waiting for an interval"


def test_a_later_ok_touch_clears_the_failure_and_its_note(client, mine):
    token, page = mine
    touch(client, token, page.id, status="failed", note="boom")
    touch(client, token, page.id)
    row = read(client, token, page.id)
    assert (row["check_status"], row["check_note"], row["lapsed"]) == (
        "ok",
        None,
        False,
    )


def test_a_note_is_capped_at_200_characters(client, mine):
    token, page = mine
    touch(client, token, page.id, status="failed", note="x" * 500)
    assert len(read(client, token, page.id)["check_note"]) <= 200


def test_a_bad_status_is_a_422(client, mine):
    token, page = mine
    assert touch(client, token, page.id, status="great").status_code == 422


# --- if_revision --------------------------------------------------------------


def test_if_revision_vouches_only_for_the_page_it_names(client, db, mine):
    token, page = mine
    assert touch(client, token, page.id, if_revision=page.revision).status_code == 200
    assert touch(client, token, page.id, if_revision="1").status_code == 200
    moved = store.patch(db, "seat", page.id, OTHER, page.revision)
    assert touch(client, token, page.id, if_revision=page.revision).status_code == 409
    assert touch(client, token, page.id, if_revision=moved.revision).status_code == 200


def test_a_refused_touch_writes_nothing(client, db, mine):
    token, page = mine
    store.patch(db, "seat", page.id, OTHER, page.revision)
    with db.connect() as conn:
        conn.execute(
            "update artifact set checked_at = null, check_status = null where id = %s",
            (page.id,),
        )
    out = touch(client, token, page.id, if_revision=page.revision, status="failed")
    assert out.status_code == 409
    row = read(client, token, page.id)
    assert row["checked_at"] is None and row["check_status"] is None


# --- scoping ------------------------------------------------------------------


def test_not_yours_answers_exactly_as_no_such_page(client, db, mine):
    token, page = mine
    stranger, _ = keys.mint(db, agent="other")
    theirs = touch(client, stranger, page.id)
    nobody = touch(client, stranger, "doesnotexist")
    assert theirs.status_code == nobody.status_code == 404
    assert theirs.json() == nobody.json()
    before = read(client, token, page.id)["checked_at"]
    touch(client, stranger, page.id, status="failed")
    assert read(client, token, page.id)["check_status"] == "ok", "nothing was written"
    assert read(client, token, page.id)["checked_at"] == before


def test_a_deleted_page_cannot_be_touched(client, db, mine):
    token, page = mine
    store.set_deleted(db, page.id, True, agent="seat")
    assert touch(client, token, page.id).status_code == 404


def test_touch_needs_a_key(client, mine):
    _, page = mine
    assert client.post(f"/api/v1/artifacts/{page.id}/touch", json={}).status_code == 401


def test_a_retry_with_the_same_idempotency_key_is_the_same_answer(client, mine):
    token, page = mine
    headers = {**auth(token), "Idempotency-Key": "k1"}
    first = client.post(f"/api/v1/artifacts/{page.id}/touch", json={}, headers=headers)
    second = client.post(f"/api/v1/artifacts/{page.id}/touch", json={}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


# --- any publish is a check ---------------------------------------------------


def test_publishing_records_a_check(client, mine):
    token, page = mine
    row = read(client, token, page.id)
    assert row["check_status"] == "ok" and row["checked_at"] is not None


def test_an_identical_republish_records_a_check_and_mints_no_version(client, db, mine):
    token, page = mine
    with db.connect() as conn:
        conn.execute(
            "update artifact set check_status = 'failed', check_note = 'x'"
            " where id = %s",
            (page.id,),
        )
    age(db, page.id, hours=3)
    old = read(client, token, page.id)
    again = store.patch(db, "seat", page.id, PAGE, page.revision)
    assert again.unchanged
    row = read(client, token, page.id)
    assert row["checked_at"] > old["checked_at"]
    assert (row["check_status"], row["check_note"]) == ("ok", None)
    assert versions(db, page.id) == 1
    assert row["updated_at"] == old["updated_at"], "still not a change"


def test_a_real_update_and_a_section_update_record_a_check(client, db, mine):
    token, page = mine
    age(db, page.id, hours=3)
    old = read(client, token, page.id)["checked_at"]
    moved = store.patch(db, "seat", page.id, OTHER, page.revision)
    mid = read(client, token, page.id)["checked_at"]
    assert mid > old
    age(db, page.id, hours=3)
    store.patch_section(db, "seat", page.id, "a", "replace", "z")
    assert read(client, token, page.id)["checked_at"] > mid
    assert moved.version == 2


# --- declaring an interval ----------------------------------------------------


def test_publish_and_patch_declare_and_clear_the_interval(client, db):
    token, _ = keys.mint(db, agent="seat")
    made = client.post(
        "/api/v1/artifacts",
        json={"title": "T", "body": PAGE, "refresh_every": 1800},
        headers=auth(token),
    )
    assert made.status_code == 201
    pid = made.json()["id"]
    assert read(client, token, pid)["refresh_every"] == 1800
    rev = read(client, token, pid)["revision"]

    def patch(**extra):
        return client.patch(
            f"/api/v1/artifacts/{pid}",
            json={"body": PAGE, "expected_revision": rev, **extra},
            headers=auth(token),
        )

    assert patch().status_code == 200
    assert read(client, token, pid)["refresh_every"] == 1800, "omitted keeps it"
    assert patch(refresh_every=7200).status_code == 200
    assert read(client, token, pid)["refresh_every"] == 7200
    assert patch(refresh_every=0).status_code == 200
    assert read(client, token, pid)["refresh_every"] is None, "0 clears"
    assert patch(refresh_every=600).status_code == 200
    assert patch(refresh_every=None).status_code == 200
    assert read(client, token, pid)["refresh_every"] is None, "null clears"


def test_touch_declares_and_clears_the_interval_and_counts_as_a_check(client, db, mine):
    token, page = mine
    age(db, page.id, days=30)
    got = touch(client, token, page.id, refresh_every=1800).json()
    assert got["refresh_every"] == 1800 and got["lapses_at"] is not None
    row = read(client, token, page.id)
    assert row["lapsed"] is False, "declaring on an old page does not flag it at once"
    touch(client, token, page.id, refresh_every=0)
    assert read(client, token, page.id)["refresh_every"] is None
    touch(client, token, page.id, refresh_every=900)
    touch(client, token, page.id)
    assert read(client, token, page.id)["refresh_every"] == 900, "omitted keeps it"


@pytest.mark.parametrize("value", [1, 59, 31_536_001, -5])
def test_an_interval_outside_60s_to_a_year_is_a_422_naming_the_field(
    client, mine, value
):
    token, page = mine
    out = touch(client, token, page.id, refresh_every=value)
    assert out.status_code == 422
    assert out.json()["detail"]["field"] == "refresh_every"
    made = client.post(
        "/api/v1/artifacts",
        json={"title": "T", "body": PAGE, "refresh_every": value},
        headers=auth(token),
    )
    assert made.status_code == 422
    assert made.json()["detail"]["field"] == "refresh_every"


# --- lapse --------------------------------------------------------------------


def test_lapse_is_two_and_a_half_intervals_of_silence(client, db, mine):
    token, page = mine
    touch(client, token, page.id, refresh_every=3600)
    age(db, page.id, seconds=int(3600 * 2.4))
    assert read(client, token, page.id)["lapsed"] is False
    age(db, page.id, seconds=int(3600 * 2.6))
    row = read(client, token, page.id)
    assert row["lapsed"] is True
    assert row["lapses_at"] is not None


def test_no_interval_never_lapses(client, db, mine):
    token, page = mine
    age(db, page.id, days=400)
    assert read(client, token, page.id)["lapsed"] is False


def test_an_archived_page_is_never_lapsed(client, db, mine):
    token, page = mine
    touch(client, token, page.id, refresh_every=60)
    age(db, page.id, days=2)
    store.set_archived(db, page.id, True, agent="seat")
    assert read(client, token, page.id)["lapsed"] is False


def test_the_last_good_check_is_the_later_of_change_and_check():
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    changed = now - timedelta(hours=1)
    state = store.check_state(
        refresh_every=1800,
        checked_at=now - timedelta(days=1),
        check_status="ok",
        changed_at=changed,
        archived_at=None,
        now=now,
    )
    assert state["lapsed"] is False, "a recent change is a recent vouching"
    assert state["lapses_at"] == changed + timedelta(seconds=4500)


def test_listings_carry_the_same_fields(client, db, mine):
    token, page = mine
    touch(client, token, page.id, refresh_every=600, status="failed", note="n")
    for url in ("/api/v1/artifacts", "/api/v1/artifacts?scope=all"):
        rows = client.get(url, headers=auth(token)).json()
        row = next(r for r in rows if r["id"] == page.id)
        assert row["refresh_every"] == 600 and row["check_status"] == "failed"
        assert row["lapsed"] is True and row["check_note"] == "n"
        assert row["changed_at"] is not None and row["checked_at"] is not None
