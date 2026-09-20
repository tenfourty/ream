"""A named privilege held by a seat, and the one thing it unlocks.

The privilege exists because of a measured failure, not a hypothetical: on
20 September 2026 a question posed on one agent's page sat unanswered for
seventeen hours. Nothing could see it except the seat that asked, so the
operator's answer to "what is waiting on Jeremy" was only ever as good as what
each agent remembered to report.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, grants, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"
_OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def client(db):
    return TestClient(create_gallery_app(db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _asks(db, agent: str, question: str = "ship it?"):
    published = store.publish(db, agent, f"{agent}'s board", PAGE)
    return published, decisions.ask(
        db,
        agent=agent,
        artifact_id=published.id,
        question=question,
        options=_OPTIONS,
    )


def test_a_seat_holds_no_scope_until_someone_grants_one(db):
    assert grants.has(db, "ops-seat", grants.READ_ALL_DECISIONS) is False
    assert grants.list_all(db) == []


def test_granting_is_idempotent_and_revoking_is_final(db):
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)

    assert grants.has(db, "ops-seat", grants.READ_ALL_DECISIONS) is True
    assert len(grants.list_all(db)) == 1

    grants.revoke(db, "ops-seat", grants.READ_ALL_DECISIONS)
    assert grants.has(db, "ops-seat", grants.READ_ALL_DECISIONS) is False


def test_a_scope_this_service_does_not_define_is_refused_not_stored(db):
    """A typo that is stored looks exactly like a privilege that is simply never
    checked -- and it would sit in the table being reviewed as if it meant
    something."""
    with pytest.raises(grants.UnknownScope):
        grants.grant(db, "ops-seat", "decisions:read-al")

    assert grants.list_all(db) == []


def test_revoking_works_for_a_scope_the_service_no_longer_defines(db):
    """Taking a privilege away must never depend on the service still defining
    it, or a rename strands rows nobody can delete."""
    with db.connect() as conn:
        conn.execute(
            "insert into seat_grant (agent, scope) values (%s, %s)",
            ("ops-seat", "decisions:read-everything-forever"),
        )

    grants.revoke(db, "ops-seat", "decisions:read-everything-forever")

    assert grants.list_all(db) == []


def test_a_grant_belongs_to_the_seat_not_to_one_key(db, client):
    """Rotating a key must not silently drop the privilege -- that would fail
    open in the worst way: quietly, at the moment someone did the right thing."""
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    _asks(db, "someone-else")

    first, _ = keys.mint(db, agent="ops-seat")
    second, _ = keys.mint(db, agent="ops-seat")

    for token in (first, second):
        response = client.get("/api/v1/decisions?scope=all", headers=auth(token))
        assert response.status_code == 200
        assert len(response.json()) == 1


def test_without_the_grant_asking_for_every_seat_is_refused_not_narrowed(db, client):
    """The dangerous answer here is 200 with a short list. A caller that asked
    for everything and got its own back would believe it -- the same lie as an
    empty feed from a broken wait, which reads as "nothing is waiting"."""
    token, _ = keys.mint(db, agent="ops-seat")
    _asks(db, "someone-else")

    response = client.get("/api/v1/decisions?scope=all", headers=auth(token))

    assert response.status_code == 403


def test_the_operators_view_names_the_page_the_seat_and_the_age(db, client):
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    token, _ = keys.mint(db, agent="ops-seat")
    published, asked = _asks(db, "warpgate-seat", question="skip the phase?")

    row = client.get("/api/v1/decisions?scope=all", headers=auth(token)).json()[0]

    assert row["id"] == asked.id
    assert row["artifact_id"] == published.id
    assert row["agent"] == "warpgate-seat"
    assert row["artifact_title"] == "warpgate-seat's board"
    assert row["question"] == "skip the phase?"
    # Computed by the database that stamped it, so a consumer's clock can never
    # disagree with the service about how long Jeremy has been kept waiting.
    assert row["age_seconds"] >= 0


def test_the_operators_view_is_oldest_first(db, client):
    """The opposite order from an agent's own feed, deliberately: an agent wants
    the newest answer, a queue in front of Jeremy wants the thing that has been
    waiting longest, because that is the one most likely to have been
    forgotten."""
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    token, _ = keys.mint(db, agent="ops-seat")
    _published, first = _asks(db, "seat-a", question="oldest")
    _published2, second = _asks(db, "seat-b", question="newest")

    rows = client.get("/api/v1/decisions?scope=all", headers=auth(token)).json()

    assert [r["id"] for r in rows] == [first.id, second.id]


def test_an_answered_or_closed_question_is_not_waiting_on_him(db, client):
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    token, _ = keys.mint(db, agent="ops-seat")
    _p1, answered = _asks(db, "seat-a", question="answered one")
    _p2, closed = _asks(db, "seat-b", question="withdrawn one")
    _p3, open_one = _asks(db, "seat-c", question="still open")
    decisions.answer(db, answered.id, letter="a", by="jeremy")
    decisions.close(db, "seat-b", closed.id)

    rows = client.get("/api/v1/decisions?scope=all", headers=auth(token)).json()

    assert [r["id"] for r in rows] == [open_one.id]


def test_an_ordinary_seat_still_sees_only_its_own(db, client):
    token, _ = keys.mint(db, agent="seat-a")
    _p1, mine = _asks(db, "seat-a")
    _asks(db, "seat-b")

    rows = client.get("/api/v1/decisions", headers=auth(token)).json()

    assert [r["id"] for r in rows] == [mine.id]


def test_a_scope_that_is_not_a_word_this_route_knows_is_refused_at_the_edge(db, client):
    token, _ = keys.mint(db, agent="ops-seat")

    response = client.get("/api/v1/decisions?scope=everything", headers=auth(token))

    assert response.status_code == 422
