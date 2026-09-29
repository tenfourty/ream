import json

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _publish(client, token: str, title: str = "Board") -> dict:
    return client.post(
        "/api/v1/artifacts", json={"title": title, "body": PAGE}, headers=auth(token)
    ).json()


# ---------------------------------------------------------------------------
# validate_anchor -- unit level, so the "non-dict" shape (which a Pydantic
# `dict | None` field would refuse before it ever reached our code) is still
# covered directly, alongside every shape that IS a dict and does reach it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "anchor",
    [
        pytest.param({"kind": "bogus"}, id="unknown-kind"),
        pytest.param({"kind": "point", "x": 1.5, "y": 0.5}, id="x-out-of-range"),
        pytest.param({"kind": "point", "x": -0.001, "y": 0.5}, id="x-below-zero"),
        pytest.param(
            {"kind": "range", "path": "p", "start": 5, "end": 5}, id="start-eq-end"
        ),
        pytest.param(
            {"kind": "range", "path": "p", "start": 5, "end": 1}, id="start-gt-end"
        ),
        pytest.param(
            {"kind": "range", "path": "p" * 513, "start": 0, "end": 1},
            id="over-long-path",
        ),
        pytest.param("not-a-dict", id="a-string"),
        pytest.param(["kind", "page"], id="a-list"),
        pytest.param(123, id="a-number"),
        pytest.param({"kind": "page", "extra": "nope"}, id="extra-key-on-page"),
        pytest.param({"kind": "point", "x": 0.5}, id="missing-key-on-point"),
    ],
)
def test_each_rejected_anchor_shape_names_only_anchor(anchor):
    with pytest.raises(comments.InvalidAnchor) as exc_info:
        comments.validate_anchor(anchor)
    assert exc_info.value.field == "anchor"


def test_anchor_none_defaults_to_page():
    assert comments.validate_anchor(None) == {"kind": "page"}


@pytest.mark.parametrize(
    "anchor",
    [
        {"kind": "page"},
        {"kind": "point", "x": 0.0, "y": 1.0},
        {"kind": "range", "path": "src/x.py", "start": 0, "end": 5},
    ],
)
def test_each_accepted_anchor_shape_round_trips(anchor):
    assert comments.validate_anchor(anchor) == anchor


def test_a_bad_anchor_over_the_api_is_422_naming_only_anchor_and_never_echoes_it(
    client, db
):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    marker = "UNIQUE_MARKER_should_never_be_echoed"
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "hello", "anchor": {"kind": "bogus", "note": marker}},
        headers=auth(token),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "anchor"
    assert marker not in response.text
    assert "bogus" not in response.text


def test_a_surrogate_split_anchor_over_the_api_is_422_not_500(client, db):
    """Final review, Item 1: a range anchor whose prefix/suffix cuts an astral
    emoji's UTF-16 pair in half (exactly what the page shim's own 32-char
    `slice` can produce at the boundary) used to reach `Jsonb` and 500 there.
    It must come back 422, naming `anchor`, like any other anchor the service
    cannot use -- never a 500, and the mark is never half-stored."""
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    # httpx's own `json=` convenience encodes with `ensure_ascii=False` and
    # would raise UnicodeEncodeError client-side before a request is even
    # sent -- exactly the bug this refusal exists to catch, just happening a
    # hop too early to prove the server's own behaviour. `ensure_ascii=True`
    # renders the lone surrogate as the literal, all-ASCII `\udfe2` escape,
    # which travels as ordinary bytes and is only turned back into that one
    # code unit when the SERVER's own JSON parser reads the body -- exactly
    # what a real client sending malformed-but-valid-JSON text would produce.
    raw = json.dumps(
        {
            "body": "hello",
            "quote": "hi",
            "anchor": {
                "kind": "range",
                "path": "p",
                "start": 0,
                "end": 2,
                "prefix": "\udfe2 all",
            },
        },
        ensure_ascii=True,
    ).encode("ascii")
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        content=raw,
        headers={**auth(token), "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "anchor"


def test_a_surrogate_split_path_over_the_api_is_422_not_500(client, db):
    """Follow-up to the final review (homelab-ij8.111): `path` reaches `Jsonb`
    the same way `prefix`/`suffix` already do, so a lone surrogate there must
    come back 422 naming `anchor`, never a 500. Same ASCII-escaped body trick
    as the prefix/suffix version above, so the server really receives a lone
    surrogate rather than httpx raising client-side first."""
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    raw = json.dumps(
        {
            "body": "hello",
            "quote": "hi",
            "anchor": {
                "kind": "range",
                "path": "p\udfe2",
                "start": 0,
                "end": 2,
            },
        },
        ensure_ascii=True,
    ).encode("ascii")
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        content=raw,
        headers={**auth(token), "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "anchor"


# ---------------------------------------------------------------------------
# Root + reply round trip, and inheritance of the root's anchor/quote.
# ---------------------------------------------------------------------------


def test_a_root_comment_and_a_reply_round_trip(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)

    root = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "what does this mean?"},
        headers=auth(token),
    )
    assert root.status_code == 201
    root_body = root.json()
    assert root_body["thread_root"] is None
    assert root_body["author_kind"] == "agent"
    assert root_body["author"] == "ops-seat"
    assert root_body["version_n"] == page["version"]
    assert root_body["anchor"] == {"kind": "page"}
    assert root_body["resolved_at"] is None

    reply = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "it means X", "thread_root": root_body["id"]},
        headers=auth(token),
    )
    assert reply.status_code == 201
    reply_body = reply.json()
    assert reply_body["thread_root"] == root_body["id"]

    listed = client.get(
        f"/api/v1/artifacts/{page['id']}/comments", headers=auth(token)
    ).json()
    assert [row["id"] for row in listed] == [root_body["id"], reply_body["id"]]


def test_a_reply_inherits_the_roots_anchor_and_quote_not_its_own(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)

    root = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={
            "body": "root comment",
            "anchor": {"kind": "point", "x": 0.2, "y": 0.3},
            "quote": "the original quote",
        },
        headers=auth(token),
    ).json()

    reply = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={
            "body": "reply comment",
            "thread_root": root["id"],
            "anchor": {"kind": "page"},
            "quote": "an ignored quote",
        },
        headers=auth(token),
    )
    assert reply.status_code == 201
    reply_body = reply.json()
    assert (
        reply_body["anchor"] == root["anchor"] == {"kind": "point", "x": 0.2, "y": 0.3}
    )
    assert reply_body["quote"] == root["quote"] == "the original quote"


def test_a_reply_naming_an_unknown_or_non_root_thread_is_refused(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)

    unknown = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "orphan reply", "thread_root": "no-such-comment-id"},
        headers=auth(token),
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["field"] == "thread_root"

    root = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "root"},
        headers=auth(token),
    ).json()
    reply = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "a reply", "thread_root": root["id"]},
        headers=auth(token),
    ).json()
    reply_to_reply = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "reply to a reply", "thread_root": reply["id"]},
        headers=auth(token),
    )
    assert reply_to_reply.status_code == 422
    assert reply_to_reply.json()["detail"]["field"] == "thread_root"


# ---------------------------------------------------------------------------
# Size caps and secrets -- the same guards a page publish goes through.
# ---------------------------------------------------------------------------


def test_an_over_long_body_is_refused_with_413(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    body = "a" * (comments.MAX_COMMENT_BYTES + 1)
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": body},
        headers=auth(token),
    )
    assert response.status_code == 413
    assert response.json()["detail"]["field"] == "body"


def test_an_over_long_quote_is_refused_with_413(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    quote = "a" * (comments.MAX_QUOTE_BYTES + 1)
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "fine", "quote": quote},
        headers=auth(token),
    )
    assert response.status_code == 413
    assert response.json()["detail"]["field"] == "quote"


def test_a_comment_carrying_a_secret_is_refused_422_like_a_publish(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": f"rotate this: {secret}"},
        headers=auth(token),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == {
        "refused": [{"field": "body", "shape": "aws_access_key_id", "line": 1}]
    }
    assert secret not in response.text


def test_a_secret_in_the_quote_is_also_refused(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "fine", "quote": secret},
        headers=auth(token),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["refused"][0]["field"] == "quote"
    assert secret not in response.text


# ---------------------------------------------------------------------------
# Ownership -- another agent's artifact is 404 everywhere, never a hint.
# ---------------------------------------------------------------------------


def test_another_agents_artifact_is_404_on_read_write_and_resolve(client, db):
    token_a, _ = keys.mint(db, agent="seat-a")
    token_b, _ = keys.mint(db, agent="seat-b")
    page = _publish(client, token_a)
    mine = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "a comment"},
        headers=auth(token_a),
    ).json()

    assert (
        client.get(
            f"/api/v1/artifacts/{page['id']}/comments", headers=auth(token_b)
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/artifacts/{page['id']}/comments",
            json={"body": "a sneaky comment"},
            headers=auth(token_b),
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/comments/{mine['id']}/resolve", headers=auth(token_b)
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Resolving: idempotent, thread-wide, and hides the whole thread once done.
# ---------------------------------------------------------------------------


def test_resolve_is_idempotent(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    root = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "root"},
        headers=auth(token),
    ).json()

    first = client.post(f"/api/v1/comments/{root['id']}/resolve", headers=auth(token))
    assert first.status_code == 200
    assert first.json()["resolved_at"] is not None
    assert first.json()["resolved_by"] == "ops-seat"

    second = client.post(f"/api/v1/comments/{root['id']}/resolve", headers=auth(token))
    assert second.status_code == 200
    assert second.json()["resolved_at"] == first.json()["resolved_at"]


def test_resolving_a_reply_resolves_the_whole_thread(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    root = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "root"},
        headers=auth(token),
    ).json()
    reply_1 = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "reply 1", "thread_root": root["id"]},
        headers=auth(token),
    ).json()
    reply_2 = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "reply 2", "thread_root": root["id"]},
        headers=auth(token),
    ).json()

    response = client.post(
        f"/api/v1/comments/{reply_1['id']}/resolve", headers=auth(token)
    )
    assert response.status_code == 200
    assert response.json()["resolved_at"] is not None

    listed = client.get(
        f"/api/v1/artifacts/{page['id']}/comments?include_resolved=true",
        headers=auth(token),
    ).json()
    by_id = {row["id"]: row for row in listed}
    assert by_id[root["id"]]["resolved_at"] is not None
    assert by_id[reply_1["id"]]["resolved_at"] is not None
    assert by_id[reply_2["id"]]["resolved_at"] is not None


def test_include_resolved_hides_a_resolved_threads_root_and_replies(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    root = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "root"},
        headers=auth(token),
    ).json()
    reply = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "reply", "thread_root": root["id"]},
        headers=auth(token),
    ).json()
    other_root = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "unrelated, unresolved thread"},
        headers=auth(token),
    ).json()

    client.post(f"/api/v1/comments/{root['id']}/resolve", headers=auth(token))

    default = client.get(
        f"/api/v1/artifacts/{page['id']}/comments", headers=auth(token)
    ).json()
    default_ids = {row["id"] for row in default}
    assert root["id"] not in default_ids
    assert reply["id"] not in default_ids
    assert other_root["id"] in default_ids

    everything = client.get(
        f"/api/v1/artifacts/{page['id']}/comments?include_resolved=true",
        headers=auth(token),
    ).json()
    everything_ids = {row["id"] for row in everything}
    assert {root["id"], reply["id"], other_root["id"]} <= everything_ids


def test_a_late_reply_on_an_already_resolved_thread_is_still_hidden(client, db):
    """A reply is born with its own resolved_at null even when its thread was
    resolved before it existed -- visibility must key off the ROOT, not the
    reply's own column, or this one row would leak through include_resolved=False."""
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    root = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "root"},
        headers=auth(token),
    ).json()
    client.post(f"/api/v1/comments/{root['id']}/resolve", headers=auth(token))

    late_reply = client.post(
        f"/api/v1/artifacts/{page['id']}/comments",
        json={"body": "late reply", "thread_root": root["id"]},
        headers=auth(token),
    ).json()
    assert late_reply["resolved_at"] is None  # not retroactively stamped

    default = client.get(
        f"/api/v1/artifacts/{page['id']}/comments", headers=auth(token)
    ).json()
    assert late_reply["id"] not in {row["id"] for row in default}


# ---------------------------------------------------------------------------
# The cross-artifact polling surface.
# ---------------------------------------------------------------------------


def test_list_for_agent_spans_two_artifacts_newest_first(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page_1 = _publish(client, token, title="Board 1")
    page_2 = _publish(client, token, title="Board 2")
    first = client.post(
        f"/api/v1/artifacts/{page_1['id']}/comments",
        json={"body": "on board 1"},
        headers=auth(token),
    ).json()
    second = client.post(
        f"/api/v1/artifacts/{page_2['id']}/comments",
        json={"body": "on board 2"},
        headers=auth(token),
    ).json()

    everything = client.get("/api/v1/comments", headers=auth(token)).json()
    assert [row["id"] for row in everything] == [second["id"], first["id"]]


def test_list_for_agent_honours_the_limit(db):
    """Exercised at the store layer: the agent API in this slice never exposes
    `limit` as a query parameter, only `list_for_agent`'s own default -- this
    pins that the parameter itself works, independent of whether anything
    above it ever passes a non-default value."""
    store.publish(db, "seat-x", "Board 1", PAGE)
    artifact_2 = store.publish(db, "seat-x", "Board 2", PAGE)
    comments.add(
        db,
        artifact_id=artifact_2.id,
        author_kind="agent",
        author="seat-x",
        body="first",
        agent="seat-x",
    )
    comments.add(
        db,
        artifact_id=artifact_2.id,
        author_kind="agent",
        author="seat-x",
        body="second",
        agent="seat-x",
    )
    comments.add(
        db,
        artifact_id=artifact_2.id,
        author_kind="agent",
        author="seat-x",
        body="third",
        agent="seat-x",
    )

    everything = comments.list_for_agent(db, "seat-x")
    assert len(everything) == 3

    limited = comments.list_for_agent(db, "seat-x", limit=1)
    assert len(limited) == 1
    assert limited[0].body == "third"


# ---------------------------------------------------------------------------
# The gallery's own path: `agent=None`, only existence is checked.
# ---------------------------------------------------------------------------


def test_agent_none_only_checks_existence_not_ownership(db):
    published = store.publish(db, "seat-a", "Board", PAGE)
    row = comments.add(
        db,
        artifact_id=published.id,
        author_kind="human",
        author="jeremy",
        body="a note from Jeremy",
        agent=None,
    )
    assert row.author_kind == "human"
    assert row.author == "jeremy"

    with pytest.raises(store.NotFound):
        comments.add(
            db,
            artifact_id="no-such-artifact-id",
            author_kind="human",
            author="jeremy",
            body="a note",
            agent=None,
        )


# --- who may resolve, and the default that must never widen -------------------


def test_a_key_cannot_resolve_another_seats_thread(db):
    """`resolve` grew an `agent=None` path so the GALLERY can settle a thread
    for Jeremy, who owns nothing in the agent column's sense. None means "skip
    the ownership check", which is right there and is a privilege escalation
    anywhere else.

    This test exists because I nearly shipped that: the keyed API called
    `resolve(by=agent)` and, the moment the parameter was added with a None
    default, that call silently stopped checking ownership. It now passes
    `agent=` explicitly and this asserts the behaviour rather than the spelling.
    """
    keys.mint(db, agent="seat-a")
    keys.mint(db, agent="seat-b")
    page = store.publish(db, "seat-a", "A's page", PAGE)
    mine = comments.add(
        db, artifact_id=page.id, author_kind="agent", author="seat-a", body="ours"
    )

    with pytest.raises(store.NotFound):
        comments.resolve(db, mine.id, by="seat-b", agent="seat-b")

    still = comments.list_for_artifact(db, page.id, include_resolved=True)[0]
    assert still.resolved_at is None, "another seat settled a thread it does not own"


def test_the_owning_seat_can_resolve_its_own(db):
    keys.mint(db, agent="seat-a")
    page = store.publish(db, "seat-a", "A's page", PAGE)
    mine = comments.add(
        db, artifact_id=page.id, author_kind="agent", author="seat-a", body="ours"
    )
    done = comments.resolve(db, mine.id, by="seat-a", agent="seat-a")
    assert done.resolved_at is not None


def test_the_gallery_resolves_for_jeremy_who_owns_nothing(db):
    """No artifact is owned by "jeremy", so an ownership-checking resolve can
    never succeed for him. Every press of Mark settled would 404 -- which is
    exactly what happened, and was found by rendering the page rather than by
    a test."""
    keys.mint(db, agent="seat-a")
    page = store.publish(db, "seat-a", "A's page", PAGE)
    thread = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="a question"
    )

    with pytest.raises(store.NotFound):
        comments.resolve(db, thread.id, by="jeremy", agent="jeremy")

    done = comments.resolve(db, thread.id, by="jeremy", agent=None)
    assert done.resolved_at is not None
    assert done.resolved_by == "jeremy"


# --- the text a comment is about, and whether it is still there ---------------
#
# homelab-ij8.18's re-anchoring rule. A comment is words about OTHER words, and
# a page here is republished constantly -- so the words it was about can be
# gone while the comment sits where they used to be. The design says it
# plainly: never silently move a comment onto text it was not about.


QUOTED = (
    "<!doctype html><html><head><title>Board</title></head><body>"
    "<p>The backups run at midnight.</p></body></html>"
)


def test_a_quote_still_on_the_page_reads_as_current(client, db):
    art = store.publish(db, "seat", "Board", QUOTED)
    comments.add(
        db,
        artifact_id=art.id,
        author_kind="human",
        author="jeremy",
        body="Why then?",
        anchor={"kind": "range", "path": "/p[1]", "start": 0, "end": 29},
        quote="The backups run at midnight.",
    )
    [row] = comments.list_for_artifact(db, art.id)
    assert row.quote_state == "current"


def test_a_quote_the_page_has_moved_past_says_so(client, db):
    art = store.publish(db, "seat", "Board", QUOTED)
    comments.add(
        db,
        artifact_id=art.id,
        author_kind="human",
        author="jeremy",
        body="Why then?",
        quote="The backups run at midnight.",
    )
    store.patch(
        db,
        "seat",
        art.id,
        QUOTED.replace("run at midnight", "run at four"),
        art.revision,
    )
    [row] = comments.list_for_artifact(db, art.id)
    assert row.quote_state == "changed"
    assert row.version_n == 1, "and it stays attached to the version it was made on"


def test_markup_around_a_quote_does_not_make_it_stale(client, db):
    """A quote is what a reader selected, so it never carries the tags that may
    sit inside it. Comparing HTML would call an untouched sentence changed the
    moment a word in it was emphasised."""
    art = store.publish(db, "seat", "Board", QUOTED)
    comments.add(
        db,
        artifact_id=art.id,
        author_kind="human",
        author="jeremy",
        body="Why then?",
        quote="The backups run at midnight.",
    )
    store.patch(
        db,
        "seat",
        art.id,
        QUOTED.replace("midnight", "<em>midnight</em>"),
        art.revision,
    )
    assert comments.list_for_artifact(db, art.id)[0].quote_state == "current"


def test_whitespace_and_case_are_not_changes_either(client, db):
    art = store.publish(db, "seat", "Board", QUOTED)
    comments.add(
        db,
        artifact_id=art.id,
        author_kind="human",
        author="jeremy",
        body="Why then?",
        quote="the   BACKUPS\n run at midnight.",
    )
    assert comments.list_for_artifact(db, art.id)[0].quote_state == "current"


def test_a_whole_page_comment_is_never_called_stale(client, db):
    """Most comments quote nothing. A whole-page remark cannot go stale this
    way, and marking one would put a warning on a comment that is fine."""
    art = store.publish(db, "seat", "Board", QUOTED)
    comments.add(
        db, artifact_id=art.id, author_kind="human", author="jeremy", body="Nice page."
    )
    assert comments.list_for_artifact(db, art.id)[0].quote_state == ""


def test_a_reply_carries_the_same_state_as_its_thread(client, db):
    """A reply inherits the root's quote, so it must inherit its verdict --
    one conversation cannot be half stale."""
    art = store.publish(db, "seat", "Board", QUOTED)
    root = comments.add(
        db,
        artifact_id=art.id,
        author_kind="human",
        author="jeremy",
        body="Why then?",
        quote="The backups run at midnight.",
    )
    comments.add(
        db,
        artifact_id=art.id,
        author_kind="agent",
        author="seat",
        body="Because the disks are quiet.",
        thread_root=root.id,
        agent="seat",
    )
    store.patch(db, "seat", art.id, "<!doctype html><body>gone</body>", art.revision)
    assert [r.quote_state for r in comments.list_for_artifact(db, art.id)] == [
        "changed",
        "changed",
    ]


def test_the_state_reaches_an_agent_through_the_api(client, db):
    """An agent reading its comments has the same problem Jeremy does: it
    cannot see the page either (homelab-ij8.35), so it must be told."""
    token, _ = keys.mint(db, agent="ops-seat")
    published = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": QUOTED},
        headers=auth(token),
    ).json()
    client.post(
        f"/api/v1/artifacts/{published['id']}/comments",
        json={"body": "Why then?", "quote": "The backups run at midnight."},
        headers=auth(token),
    )
    client.patch(
        f"/api/v1/artifacts/{published['id']}",
        json={
            "body": QUOTED.replace("midnight", "four"),
            "expected_revision": published["revision"],
        },
        headers=auth(token),
    )
    got = client.get(
        f"/api/v1/artifacts/{published['id']}/comments", headers=auth(token)
    ).json()
    assert [c["quote_state"] for c in got] == ["changed"]


# --- every page's conversations, in one list ----------------------------------


def test_across_pages_puts_the_most_recently_talked_about_page_first(db):
    """Two orderings in one function, both already argued elsewhere here: the
    PAGES answer "where is something being said" so they come newest-active
    first, and the comments WITHIN a page are a conversation, so they read
    forwards."""
    quiet = store.publish(db, "seat", "Quiet board", PAGE)
    busy = store.publish(db, "seat", "Busy board", PAGE)
    comments.add(
        db, artifact_id=quiet.id, author_kind="agent", author="seat", body="first"
    )
    comments.add(
        db, artifact_id=busy.id, author_kind="agent", author="seat", body="second"
    )
    comments.add(
        db, artifact_id=busy.id, author_kind="agent", author="seat", body="third"
    )

    pages = comments.across_pages(db)

    assert [p.artifact_id for p in pages] == [busy.id, quiet.id]
    assert [c.body for c in pages[0].rows] == ["second", "third"]


def test_across_pages_leaves_out_a_deleted_page_and_flags_an_archived_one(db):
    gone = store.publish(db, "seat", "Deleted board", PAGE)
    shelved = store.publish(db, "seat", "Archived board", PAGE)
    comments.add(db, artifact_id=gone.id, author_kind="agent", author="seat", body="x")
    comments.add(
        db, artifact_id=shelved.id, author_kind="agent", author="seat", body="y"
    )
    store.set_deleted(db, gone.id, True, agent="seat")
    store.set_archived(db, shelved.id, True, agent="seat")

    pages = comments.across_pages(db)

    assert [p.artifact_id for p in pages] == [shelved.id]
    assert pages[0].archived is True


def test_across_pages_answers_whether_each_quote_is_still_there(db):
    """The same promise `list_for_artifact` makes, kept across every page at
    once: a quote that has been edited away must say so rather than sit next to
    whatever now occupies that spot."""
    art = store.publish(db, "seat", "Board", QUOTED)
    comments.add(
        db,
        artifact_id=art.id,
        author_kind="agent",
        author="seat",
        body="Why then?",
        quote="The backups run at midnight.",
    )
    other = store.publish(db, "seat", "Other", QUOTED)
    comments.add(
        db,
        artifact_id=other.id,
        author_kind="agent",
        author="seat",
        body="Still right",
        quote="The backups run at midnight.",
    )
    current = store.get_owned(db, "seat", art.id)
    store.patch(
        db, "seat", art.id, QUOTED.replace("midnight", "four"), current.revision
    )

    by_page = {p.artifact_id: p for p in comments.across_pages(db)}

    assert [c.quote_state for c in by_page[art.id].rows] == ["changed"]
    assert [c.quote_state for c in by_page[other.id].rows] == ["current"]


def test_across_pages_hides_settled_threads_unless_asked(db):
    art = store.publish(db, "seat", "Board", PAGE)
    root = comments.add(
        db, artifact_id=art.id, author_kind="human", author="jeremy", body="done"
    )
    comments.resolve(db, root.id, by="jeremy", agent=None)

    assert comments.across_pages(db) == []
    assert [
        p.artifact_id for p in comments.across_pages(db, include_resolved=True)
    ] == [art.id]


def test_an_agents_whole_feed_says_whether_each_quote_is_still_there(client, db):
    """The per-page route has answered this since 21 Sep and the cross-page
    feed returned "" for every row, so the same comment read two ways gave an
    agent two different answers and one of them was always wrong."""
    token, _ = keys.mint(db, agent="ops-seat")
    published = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": QUOTED},
        headers=auth(token),
    ).json()
    client.post(
        f"/api/v1/artifacts/{published['id']}/comments",
        json={"body": "Why then?", "quote": "The backups run at midnight."},
        headers=auth(token),
    )
    client.patch(
        f"/api/v1/artifacts/{published['id']}",
        json={
            "body": QUOTED.replace("midnight", "four"),
            "expected_revision": published["revision"],
        },
        headers=auth(token),
    )

    feed = client.get("/api/v1/comments", headers=auth(token)).json()
    per_page = client.get(
        f"/api/v1/artifacts/{published['id']}/comments", headers=auth(token)
    ).json()

    assert [c["quote_state"] for c in feed] == ["changed"]
    assert [c["quote_state"] for c in feed] == [c["quote_state"] for c in per_page]
