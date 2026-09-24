"""When Jeremy comments, the agent that owns the page hears straight away.

homelab-ij8.19, ruled by Jeremy 19 Sep 2026 (option a) and widened by him the
same day: EVERY interaction he has with a page nudges the agent that owns it --
answering a decision, commenting, settling a thread -- so a seat can answer
while it is still working, and anything left turns up in his digest.

The journal has named `comment_added` and `thread_resolved` since 19 Sep with
nothing producing either, which is the same shape as the comments API itself
before today: a capability complete on the side nobody was waiting on. These
tests are about the side that was missing.

Best-effort is a rule here, not a caveat: a nudge that fails must never cost a
comment. The comment is durable in the database and readable on the agent's
next turn regardless, which is why the journal row and the comment commit
together, and the push happens afterwards.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, events, nudge, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


@pytest.fixture()
def page(db):
    return store.publish(db, "seat", "Board", PAGE, session="seat/one")


def _same_origin(extra=None):
    headers = {"Sec-Fetch-Site": "same-origin"}
    headers.update(extra or {})
    return headers


def _feed(db, agent="seat", **kw):
    return events.list_for_agent(db, agent, **kw)


# --- a comment of his reaches the seat ----------------------------------------


def test_his_comment_lands_in_the_owning_agents_journal(db, page):
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="Can the chart go first?",
    )
    [event] = _feed(db)
    assert event.kind == "comment_added"
    assert event.agent == "seat", "for the seat that owns the page"
    assert event.actor == "jeremy", "caused by him, never inferred from the kind"
    assert event.note == "Can the chart go first?", "his words, whole"
    assert "chart" in event.summary


def test_the_event_names_the_comment_so_the_thread_can_be_read(db, page):
    written = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="This bit.",
        anchor={"kind": "range", "path": "/p[1]", "start": 0, "end": 4},
        quote="This",
    )
    [event] = _feed(db)
    assert event.ref_id == written.id, (
        "the id of the comment, so the agent can fetch the quote and the anchor "
        "rather than the service having to flatten them into a summary"
    )


def test_the_event_carries_the_session_that_published_the_page(db, page):
    """Three seats share one key, so `agent` cannot separate them. The label is
    the session that published the page, which is the seat whose work it is."""
    comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="Hm."
    )
    assert _feed(db)[0].session == "seat/one"
    assert _feed(db, session="seat/one"), "and a seat filtering to its own sees it"


def test_a_reply_of_his_into_a_thread_is_an_event_too(db, page):
    root = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="First?"
    )
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="Actually, second.",
        thread_root=root.id,
    )
    assert [e.kind for e in _feed(db)] == ["comment_added", "comment_added"]
    assert _feed(db)[1].note == "Actually, second."


# --- but a seat is never woken by itself --------------------------------------


def test_the_owning_agents_own_comment_is_not_an_event(db, page):
    """An event means "something happened on your page that you did not do".
    A watcher that cries wolf trains its reader to stop looking, and the one
    wake that matters then looks exactly like the noise."""
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body="Noting this for later.",
        agent="seat",
    )
    assert _feed(db) == []


def test_the_owner_settling_its_own_thread_is_not_an_event(db, page):
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body="Mine.",
        agent="seat",
    )
    comments.resolve(db, root.id, by="seat", agent="seat")
    assert _feed(db) == []


# --- settling one of his ------------------------------------------------------


def test_he_settles_a_thread_and_the_seat_is_told_which(db, page):
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="Can the chart go first?",
        anchor={"kind": "range", "path": "/p[1]", "start": 0, "end": 5},
        quote="the velocity chart",
    )
    comments.resolve(db, root.id, by="jeremy")
    settled = _feed(db)[-1]
    assert settled.kind == "thread_resolved"
    assert settled.ref_id == root.id, (
        "the root, so a reply id still resolves the thread"
    )
    assert "the velocity chart" in settled.summary, (
        "the quote is how a reader remembers which conversation it was"
    )
    assert settled.actor == "jeremy"


def test_a_thread_with_no_quote_is_named_by_what_it_said(db, page):
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="Move it up.",
    )
    comments.resolve(db, root.id, by="jeremy")
    assert "Move it up." in _feed(db)[-1].summary


def test_settling_an_already_settled_thread_says_nothing_twice(db, page):
    root = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="Done?"
    )
    comments.resolve(db, root.id, by="jeremy")
    comments.resolve(db, root.id, by="jeremy")
    assert [e.kind for e in _feed(db)] == ["comment_added", "thread_resolved"]


# --- the two halves hold together ---------------------------------------------


def test_a_journal_failure_takes_the_comment_with_it(db, page, monkeypatch):
    """One transaction, both rows. A comment stored with nothing telling the
    agent is the exact failure this bead exists to close, so it must not be
    reachable by half of this succeeding."""

    def boom(*_a, **_kw):
        raise RuntimeError("journal down")

    monkeypatch.setattr(events, "append", boom)
    with pytest.raises(RuntimeError):
        comments.add(
            db, artifact_id=page.id, author_kind="human", author="jeremy", body="Lost?"
        )
    assert comments.list_for_artifact(db, page.id) == []


def test_the_push_happens_after_the_row_is_durable(db, page, monkeypatch):
    """`event_id` must name a row another agent can actually read: the nudge
    tells a seat to go and look, and a seat that looks and finds nothing has
    been sent on an errand for something that never happened."""
    seen = {}

    def spy(**kw):
        seen.update(kw)
        seen["rows_visible"] = [e.seq for e in _feed(db)]

    monkeypatch.setattr(nudge, "emit", spy)
    comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="Look."
    )
    assert seen["kind"] == "comment_added"
    assert seen["agent"] == "seat"
    assert seen["title"] == "Board"
    assert int(seen["event_id"]) in seen["rows_visible"], (
        "the row was committed and readable before the push went out"
    )


def test_a_failing_nudge_never_costs_the_comment(db, page, monkeypatch):
    """`nudge.emit` promises never to raise, and this asserts the promise from
    the caller's side rather than trusting it: commenting is what Jeremy came
    to do."""

    def boom(**_kw):
        raise RuntimeError("inbox down")

    monkeypatch.setattr(nudge, "emit", boom)
    with pytest.raises(RuntimeError):
        comments.add(
            db, artifact_id=page.id, author_kind="human", author="jeremy", body="Hi."
        )
    assert [c.body for c in comments.list_for_artifact(db, page.id)] == ["Hi."], (
        "the comment survives a broken nudge -- it is stored and journalled "
        "before the push is even attempted"
    )


# --- end to end: the held request actually ends -------------------------------


def test_a_held_waiter_is_answered_the_moment_he_comments(client, db, page):
    """The whole point of ij8.19, and the only test here that proves it: an
    agent holding /events?wait= is answered by him pressing Comment, rather
    than on its next poll. No address for the agent is needed, so this works
    for a seat that is not on this machine."""
    from artifacts import keys

    token, _ = keys.mint(db, agent="seat")
    answer = {}
    started = threading.Event()

    def hold():
        started.set()
        began = time.monotonic()
        got = client.get(
            "/api/v1/events?since=0&wait=20",
            headers={"Authorization": f"Bearer {token}"},
        )
        answer["rows"] = got.json()
        answer["waited"] = time.monotonic() - began

    thread = threading.Thread(target=hold)
    thread.start()
    started.wait(5)
    # Long enough for the holder to find nothing and settle into waiting, so
    # this cannot pass on a row that was already there.
    time.sleep(1.0)
    client.post(
        f"/c/{page.id}",
        data={"body": "While you are in there, can the chart go first?"},
        headers=_same_origin(),
        follow_redirects=False,
    )
    thread.join(20)
    assert [r["kind"] for r in answer["rows"]] == ["comment_added"]
    assert answer["waited"] < 10, "answered by the comment, not by the timeout"
