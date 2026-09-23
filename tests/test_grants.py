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


# --- what the grant covers once he has ANSWERED (homelab-9gj) --------------
#
# The grant shipped able to say "a question is waiting on Jeremy" and unable to
# say what he answered. The operator consumed it on 20 Sep 2026 and reported
# the gap the same day: it could watch a cross-seat question disappear from the
# feed -- which is how it infers an answer -- and never read the letter or the
# note, so its queue closed those rows with a generic "answered on its page"
# instead of what he actually said. Two of that day's three live decisions were
# cross-seat, so this was the common case rather than an edge.


def test_a_granted_seat_reads_the_answer_he_gave_another_seat(db, client):
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    token, _ = keys.mint(db, agent="ops-seat")
    _published, asked = _asks(db, "warpgate-seat", question="skip the phase?")
    decisions.answer(db, asked.id, letter="b", note="not until the DNS lands", by="jeremy")

    rows = client.get(
        "/api/v1/decisions?scope=all&include_answered=true", headers=auth(token)
    ).json()

    assert [r["id"] for r in rows] == [asked.id]
    assert rows[0]["answer"] == "b"
    assert rows[0]["answer_note"] == "not until the DNS lands"
    assert rows[0]["answered_at"]
    # Whose page it was, still, because a queue that cannot say which seat
    # asked cannot tell that seat what it learned.
    assert rows[0]["agent"] == "warpgate-seat"


def test_the_cross_seat_feed_still_defaults_to_what_is_waiting(db, client):
    """A REGRESSION PIN WITH A NAMED CONSUMER, not a preference. The operator's
    `ops-waiting-feed.sh` reconciles its queue against this feed by
    DISAPPEARANCE: a row it holds that stops being returned is taken as proof
    the decision is no longer open, and closed. If answered rows appeared here
    by default, every one of those rows would stay open forever."""
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    token, _ = keys.mint(db, agent="ops-seat")
    _p1, answered = _asks(db, "seat-a", question="answered one")
    _p2, open_one = _asks(db, "seat-b", question="still open")
    decisions.answer(db, answered.id, letter="a", by="jeremy")

    plain = client.get("/api/v1/decisions?scope=all", headers=auth(token)).json()

    assert [r["id"] for r in plain] == [open_one.id]


def test_open_only_beats_include_answered_on_the_cross_seat_feed(db, client):
    """Both flags in one call is a caller that has not made up its mind; the
    explicit "only what is open" wins, exactly as it does on a seat's own feed.
    Pinned because the alternative -- answered rows in a feed the caller asked
    to be open-only -- is the shape that breaks the queue above."""
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    token, _ = keys.mint(db, agent="ops-seat")
    _p1, answered = _asks(db, "seat-a", question="answered one")
    _p2, open_one = _asks(db, "seat-b", question="still open")
    decisions.answer(db, answered.id, letter="a", by="jeremy")

    rows = client.get(
        "/api/v1/decisions?scope=all&include_answered=true&open_only=true",
        headers=auth(token),
    ).json()

    assert [r["id"] for r in rows] == [open_one.id]


def test_a_granted_seat_can_ask_for_the_withdrawn_ones_too(db, client):
    """Since 21 Sep archiving or deleting a page withdraws its open questions,
    so most withdrawals are the service's doing and the asking seat never chose
    them. A queue holding such a row needs to be able to find out why."""
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    token, _ = keys.mint(db, agent="ops-seat")
    _p1, withdrawn = _asks(db, "seat-a", question="withdrawn one")
    _p2, open_one = _asks(db, "seat-b", question="still open")
    decisions.close(db, "seat-a", withdrawn.id)

    rows = client.get(
        "/api/v1/decisions?scope=all&include_closed=true", headers=auth(token)
    ).json()

    assert {r["id"] for r in rows} == {withdrawn.id, open_one.id}
    assert next(r for r in rows if r["id"] == withdrawn.id)["closed_at"]


def test_a_granted_seat_reads_another_seats_page_decisions(db, client):
    """The route the operator's enrichment actually calls. It answered 404 for
    every cross-seat page, which is why the generic note existed."""
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    token, _ = keys.mint(db, agent="ops-seat")
    published, asked = _asks(db, "warpgate-seat", question="skip the phase?")
    decisions.answer(db, asked.id, letter="a", note="yes, but keep the flag", by="jeremy")

    response = client.get(
        f"/api/v1/artifacts/{published.id}/decisions?include_answered=true",
        headers=auth(token),
    )

    assert response.status_code == 200
    assert response.json()[0]["answer"] == "a"
    assert response.json()[0]["answer_note"] == "yes, but keep the flag"


def test_without_the_grant_another_seats_page_answers_exactly_like_a_missing_one(
    db, client
):
    """The property this service protects: "not yours" and "no such page" are
    indistinguishable, so a key cannot be walked to learn which ids are real.
    Granting one seat the difference must not leak it to every other seat."""
    token, _ = keys.mint(db, agent="nosy-seat")
    published, _asked = _asks(db, "warpgate-seat")
    store.set_agents_can_read(db, published.id, False, agent=None)  # closed to other agents: homelab-ij8.105 made pages readable by default

    theirs = client.get(
        f"/api/v1/artifacts/{published.id}/decisions", headers=auth(token)
    )
    nowhere = client.get(
        "/api/v1/artifacts/zzzzzzzzzzzzzzzzzzzzzzzzzz/decisions", headers=auth(token)
    )

    assert theirs.status_code == nowhere.status_code == 404
    assert theirs.json() == nowhere.json()


def test_the_grant_does_not_conjure_pages_that_do_not_exist(db, client):
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    token, _ = keys.mint(db, agent="ops-seat")

    response = client.get(
        "/api/v1/artifacts/zzzzzzzzzzzzzzzzzzzzzzzzzz/decisions", headers=auth(token)
    )

    assert response.status_code == 404


def test_a_deleted_page_is_gone_even_for_a_granted_seat(db, client):
    """`exists` carries the same `deleted_at is null` every other read in the
    store carries. A grant widens WHOSE pages you can read, never WHICH."""
    grants.grant(db, "ops-seat", grants.READ_ALL_DECISIONS)
    token, _ = keys.mint(db, agent="ops-seat")
    published, _asked = _asks(db, "warpgate-seat")
    store.set_deleted(db, published.id, True, agent="warpgate-seat")

    response = client.get(
        f"/api/v1/artifacts/{published.id}/decisions?include_answered=true&include_closed=true",
        headers=auth(token),
    )

    assert response.status_code == 404


# --- who owes Jeremy a reply, across every seat ------------------------------
#
# 22 Sep 2026, asked for by the operator seat and approved by Jeremy the same
# morning: what has he ANSWERED that nobody has collected, across seats.
#
# THE SERVICE COULD ALREADY ANSWER IT. `GET /decisions/uncollected` has done so
# since 19 Sep, with `scope=all` behind this same grant, and it carries what a
# second spelling of the filter could not: `state`, separating an asker that is
# GONE from one that is STUCK. I added `?unacknowledged=true` to /decisions
# before finding it, and reverted that the same hour -- the route's own comment
# had already said why ("two spellings of the same privilege is how one of them
# ends up laxer than the other").
#
# Its tests live in tests/test_uncollected.py, including the cross-seat scope.
# Nothing is duplicated here; this note is the signpost, because the next person
# to want this question will look where I looked.
