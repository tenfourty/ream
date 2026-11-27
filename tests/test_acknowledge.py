"""Acknowledgement: "an agent has seen this answer".

Three facts about a decision are now distinct, and the whole point of this file
is that they stay distinct: Jeremy answered it (`answered_at`), somebody heard
that (`acknowledged_at`), and the work it caused is finished (`closed_at`). The
alert that catches a lost answer reads the second one, because the first is
about Jeremy and the third can be hours of infrastructure work away -- an alert
that waits for completion fires on decisions that are being handled correctly,
gets muted, and then misses the one that was genuinely lost.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import decisions, keys, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"
_OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def client(db):
    return TestClient(create_gallery_app(db))


def _decision(db, agent="ops-seat"):
    art = store.publish(db, agent, "Board", PAGE)
    return decisions.ask(
        db,
        agent=agent,
        artifact_id=art.id,
        question="ship it?",
        options=_OPTIONS,
    )


def test_a_fresh_decision_is_not_acknowledged(db):
    d = _decision(db)
    assert d.acknowledged_at is None
    assert d.acknowledged_by is None


def test_acknowledging_records_who_and_when(db):
    d = _decision(db)

    acked = decisions.acknowledge(db, "ops-seat", d.id)

    assert acked.acknowledged_at is not None
    assert acked.acknowledged_by == "ops-seat"
    assert acked.closed_at is None, "seen is not done"


def test_the_first_acknowledgement_is_the_one_that_counts(db):
    """Not a "latest seen" timestamp: the alert asks whether anyone heard this,
    and the first hearing answers it. A retry must not move the time forward."""
    d = _decision(db)
    first = decisions.acknowledge(db, "ops-seat", d.id)

    again = decisions.acknowledge(db, "ops-seat", d.id)

    assert again.acknowledged_at == first.acknowledged_at


def test_closing_implies_acknowledgement(db):
    """An agent that finished the work necessarily heard the answer. Doing its
    job must cost it nothing extra."""
    d = _decision(db)

    closed = decisions.close(db, "ops-seat", d.id)

    assert closed.closed_at is not None
    assert closed.acknowledged_at is not None
    assert closed.acknowledged_by == "ops-seat"


def test_closing_never_overwrites_an_earlier_acknowledgement(db):
    """The truer, earlier "seen at" survives a close that happens later."""
    d = _decision(db)
    acked = decisions.acknowledge(db, "ops-seat", d.id)

    closed = decisions.close(db, "ops-seat", d.id)

    assert closed.acknowledged_at == acked.acknowledged_at


def test_another_agent_cannot_acknowledge_and_cannot_tell_it_exists(db):
    d = _decision(db, agent="ops-seat")

    with pytest.raises(store.NotFound):
        decisions.acknowledge(db, "another-seat", d.id)


def test_an_unknown_decision_is_not_found(db):
    with pytest.raises(store.NotFound):
        decisions.acknowledge(db, "ops-seat", "nosuchdecisionidatall2345")


def test_acknowledging_before_an_answer_is_allowed_and_means_nothing_yet(db):
    """Refusing it would be a guard whose only effect is to make a retry after
    the answer arrives fail for the wrong reason. The alert only ever reads
    this field alongside `answered_at`."""
    d = _decision(db)

    acked = decisions.acknowledge(db, "ops-seat", d.id)

    assert acked.answer is None
    assert acked.acknowledged_at is not None


def test_the_route_needs_a_key_and_belongs_to_the_caller(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    d = _decision(db)

    assert client.post(f"/api/v1/decisions/{d.id}/acknowledge").status_code == 401

    ok = client.post(
        f"/api/v1/decisions/{d.id}/acknowledge",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ok.status_code == 200
    assert ok.json()["acknowledged_by"] == "ops-seat"
    assert ok.json()["closed_at"] is None


def test_an_answer_newer_than_the_ack_stamp_is_not_acknowledged(db):
    """A submit that commits between taking the stamp and the update would leave the
    decision reading acknowledged while its new answer is unseen: the update refuses an
    answer later than the stamp, and no receipt is written."""
    d = _decision(db)
    later = datetime.now(UTC) + timedelta(hours=1)
    with db.connect() as conn:
        conn.execute(
            "update decision set answered_at = %s where id = %s", (later, d.id)
        )

    got = decisions.acknowledge(db, "ops-seat", d.id)

    assert got.acknowledged_at is None and got.acknowledged_by is None
    with db.connect() as conn:
        (seen,) = conn.execute(
            "select count(*) from receipt where subject_id = %s", (d.id,)
        ).fetchone()
    assert seen == 0


def test_a_seen_receipt_for_an_answer_newer_than_the_stamp_is_refused_not_null(
    client, db
):
    """Clock skew can date an answer after the ack stamp. The receipt route used to answer
    200 with a null body there; it now refuses (409) and says why, and nothing is marked."""
    token, _ = keys.mint(db, agent="ops-seat")
    d = _decision(db)
    later = datetime.now(UTC) + timedelta(hours=1)
    with db.connect() as conn:
        conn.execute(
            "update decision set answered_at = %s where id = %s", (later, d.id)
        )

    r = client.post(
        f"/api/v1/decisions/{d.id}/receipt",
        json={"state": "seen"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 409, r.text
    assert r.json() is not None and "newer" in r.json()["detail"]
    got = decisions.get_owned(db, "ops-seat", d.id)
    assert got.acknowledged_at is None and got.receipt is None


def test_the_ack_locks_the_decision_row_before_it_takes_the_stamp(db, monkeypatch):
    """The lock alone, apart from the update's refusal of a later answer: whatever commits
    after the stamp was taken must have waited for the row, so the lock select has to run
    before the clock is read. SQLite has no row lock, so the clause is swapped for a marker
    comment and the order of the marker and the clock read is what is pinned."""
    d = _decision(db)
    events: list[str] = []

    class Spy(datetime):
        @classmethod
        def now(cls, tz=None):
            events.append("stamp")
            return datetime.now(tz)

    class Conn:
        def __init__(self, real):
            self._real = real

        def for_update(self, suffix=""):
            return f" /*lock{suffix}*/"

        def execute(self, sql, *args, **kwargs):
            if "/*lock" in sql:
                assert sql.lstrip().startswith("select") and "from decision" in sql
                events.append("lock")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(decisions, "datetime", Spy)
    with db.connect() as conn, conn.transaction():
        decisions._acknowledge(Conn(conn), "ops-seat", d.id)

    assert events[:2] == ["lock", "stamp"], events
