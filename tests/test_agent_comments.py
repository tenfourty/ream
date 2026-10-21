"""An agent comments on ANOTHER office's page (21 Oct 2026, runcove-5t3oj.36).

Two fixed rules: an agent's comment is never Jeremy's answer to a decision and
never acknowledges or closes one, and every comment shows which agent wrote it,
never as if it were him. The reach is the reach of reading: a page open to
agents, and a closed, deleted or unknown one answers one and the same 404.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, decisions, events, keys, store
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body>"
    "<h2>Start at boot</h2><p>The unit starts after the network is up.</p>"
    "<p>Retries are capped at three.</p>"
    "<h2>Shutdown</h2><p>Retries are capped at five.</p>"
    "<p>The unit stops before the network goes down.</p>"
    "</body></html>"
)
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db), base_url="https://testserver")


@pytest.fixture()
def seats(db):
    owner, _ = keys.mint(db, agent="owner")
    other, _ = keys.mint(db, agent="other")
    return {
        "owner": {"Authorization": f"Bearer {owner}"},
        "other": {"Authorization": f"Bearer {other}"},
    }


@pytest.fixture()
def page(db):
    return store.publish(db, "owner", "Boot design", PAGE)


def _post(client, seats, page_id, who="other", session=None, **body):
    headers = dict(seats[who])
    if session:
        headers["X-Artifacts-Session"] = session
    return client.post(
        f"/api/v1/artifacts/{page_id}/comments",
        json={"body": "I would cap it lower.", **body},
        headers=headers,
    )


def test_an_agent_comments_on_a_readable_page_and_the_owner_hears(
    client, db, seats, page
):
    got = _post(client, seats, page.id, session="cove/ab12")
    assert got.status_code == 201
    row = got.json()
    assert (row["author_kind"], row["author"], row["kind"]) == (
        "agent",
        "other",
        "comment",
    )
    listed = client.get(
        f"/api/v1/artifacts/{page.id}/comments", headers=seats["other"]
    ).json()
    assert [r["id"] for r in listed] == [row["id"]]
    heard = [
        e
        for e in events.list_for_agent(db, "owner")
        if e.kind == "comment_added" and e.ref_id == row["id"]
    ]
    assert len(heard) == 1 and heard[0].actor == "other"


def test_the_event_still_fires_on_a_retried_keyed_request(client, db, seats, page):
    headers = {**seats["other"], "Idempotency-Key": "k-1"}
    url = f"/api/v1/artifacts/{page.id}/comments"
    first = client.post(url, json={"body": "once"}, headers=headers)
    again = client.post(url, json={"body": "once"}, headers=headers)
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    heard = [e for e in events.list_for_agent(db, "owner") if e.kind == "comment_added"]
    assert len(heard) == 1


def test_a_closed_deleted_or_unknown_page_answers_the_same_404(client, db, seats, page):
    closed = store.publish(db, "owner", "Closed", PAGE)
    store.set_agents_can_read(db, closed.id, False, agent="owner")
    gone = store.publish(db, "owner", "Gone", PAGE)
    store.set_deleted(db, gone.id, True, agent="owner")
    answers = set()
    for page_id in (closed.id, gone.id, "no-such-id"):
        posted = _post(client, seats, page_id)
        listed = client.get(
            f"/api/v1/artifacts/{page_id}/comments", headers=seats["other"]
        )
        assert posted.status_code == listed.status_code == 404
        answers.add((posted.text, listed.text))
    assert len(answers) == 1
    assert comments.list_for_artifact(db, closed.id) == []


@pytest.mark.parametrize("kind", ["remove", "looks_good"])
def test_verdict_kinds_are_refused_on_another_offices_page(
    client, db, seats, page, kind
):
    refused = _post(
        client,
        seats,
        page.id,
        kind=kind,
        quote="Retries are capped at three.",
        anchor={"kind": "range", "path": "/p[1]", "start": 0, "end": 5},
    )
    assert refused.status_code == 422
    assert "only kind comment" in refused.json()["detail"]["detail"]
    assert comments.list_for_artifact(db, page.id) == []


def test_the_owner_keeps_its_own_marks(client, db, seats, page):
    got = _post(
        client,
        seats,
        page.id,
        who="owner",
        kind="remove",
        quote="Retries are capped at three.",
        anchor={"kind": "range", "path": "/p[1]", "start": 0, "end": 5},
    )
    assert got.status_code == 201 and got.json()["kind"] == "remove"


def test_the_session_is_stored_and_returned(client, db, seats, page):
    row = _post(client, seats, page.id, session="cove/ab12").json()
    assert row["session"] == "cove/ab12"
    [again] = client.get(
        f"/api/v1/artifacts/{page.id}/comments", headers=seats["owner"]
    ).json()
    assert again["session"] == "cove/ab12"
    unlabelled = _post(client, seats, page.id).json()
    assert unlabelled["session"] is None


def test_an_agent_comment_never_touches_a_decision(client, db, seats, page):
    asked = decisions.ask(
        db, agent="owner", artifact_id=page.id, question="ship?", options=OPTIONS
    )
    review = decisions.ask_review(db, agent="owner", artifact_id=page.id)
    for body in ("a", "yes, ship it", "looks good to me"):
        assert _post(client, seats, page.id, body=body).status_code == 201
    for decision_id in (asked.id, review.id):
        after = decisions.get_owned(db, "owner", decision_id)
        assert after.answer is None and after.answered_by is None
        assert after.acknowledged_at is None and after.closed_at is None
    # And it cannot reach the decision routes at all as someone else's.
    other = seats["other"]
    assert (
        client.post(
            f"/api/v1/decisions/{asked.id}/acknowledge", headers=other, json={}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/decisions/{asked.id}/close", headers=other, json={}
        ).status_code
        == 404
    )


def test_only_the_owner_may_settle_a_thread(client, db, seats, page):
    row = _post(client, seats, page.id).json()
    assert (
        client.post(
            f"/api/v1/comments/{row['id']}/resolve", headers=seats["other"]
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/comments/{row['id']}/resolve", headers=seats["owner"]
        ).status_code
        == 200
    )


# --- the text anchor ----------------------------------------------------------


def test_a_text_anchor_found_once_is_stored_as_a_quoted_comment(
    client, db, seats, page
):
    got = _post(
        client,
        seats,
        page.id,
        anchor={"kind": "text", "quote": "the unit starts after the NETWORK is up"},
    )
    assert got.status_code == 201
    row = got.json()
    assert row["quote"] == "the unit starts after the NETWORK is up"
    assert row["anchor"] == {"kind": "page"}
    assert row["version_n"] == 1


def test_a_text_anchor_not_found_is_refused(client, db, seats, page):
    got = _post(client, seats, page.id, anchor={"kind": "text", "quote": "no such"})
    assert got.status_code == 422
    assert "not found" in got.json()["detail"]["detail"]
    assert comments.list_for_artifact(db, page.id) == []


def test_an_ambiguous_text_anchor_is_refused_and_names_section(client, db, seats, page):
    got = _post(
        client, seats, page.id, anchor={"kind": "text", "quote": "Retries are capped"}
    )
    assert got.status_code == 422
    detail = got.json()["detail"]
    assert detail["matches"] == 2 and "section" in detail["detail"]


def test_a_section_disambiguates_a_text_anchor(client, db, seats, page):
    got = _post(
        client,
        seats,
        page.id,
        anchor={
            "kind": "text",
            "quote": "Retries are capped",
            "section": "shutdown",
        },
    )
    assert got.status_code == 201
    missing = _post(
        client,
        seats,
        page.id,
        anchor={"kind": "text", "quote": "Retries are capped", "section": "Nowhere"},
    )
    assert missing.status_code == 422
    # Found, but not under that heading.
    elsewhere = _post(
        client,
        seats,
        page.id,
        anchor={
            "kind": "text",
            "quote": "The unit stops before",
            "section": "Start at boot",
        },
    )
    assert elsewhere.status_code == 422


def test_a_quote_is_kept_when_a_later_version_drops_the_passage(
    client, db, seats, page
):
    _post(
        client,
        seats,
        page.id,
        anchor={"kind": "text", "quote": "The unit starts after the network is up"},
    )
    store.patch(
        db,
        "owner",
        page.id,
        PAGE.replace("The unit starts after", "It begins once"),
        store.get_owned(db, "owner", page.id).revision,
    )
    [row] = comments.list_for_artifact(db, page.id)
    assert row.quote_state == "changed" and row.version_n == 1


# --- the gallery --------------------------------------------------------------


def test_the_gallery_names_an_agent_comment_as_an_agent(client, db, seats, page):
    _post(client, seats, page.id, session="cove/ab12")
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="Jeremy here",
    )
    html = client.get(f"/a/{page.id}").text
    assert 'class="cwho">other (agent)</span>' in html
    assert "via session cove" in html and 'title="cove/ab12"' in html
    assert 'class="cmt agent"' in html
    assert 'class="cwho">Jeremy</span>' in html


def test_a_sqlite_file_from_before_the_session_column_is_upgraded(
    tmp_path, monkeypatch
):
    import sqlite3

    from artifacts import db as dbmod

    path = tmp_path / "old.db"
    steps = dict(dbmod._SQLITE_STEPS)
    monkeypatch.setattr(dbmod, "_SQLITE_STEPS", {})
    monkeypatch.setattr(dbmod, "SQLITE_SCHEMA_VERSION", 1)
    dbmod.Database(f"sqlite:///{path}").migrate()
    columns = lambda: [
        r[1] for r in sqlite3.connect(path).execute("pragma table_info(comment)")
    ]
    assert "session" not in columns()
    monkeypatch.setattr(dbmod, "_SQLITE_STEPS", steps)
    monkeypatch.setattr(dbmod, "SQLITE_SCHEMA_VERSION", max(steps))
    upgraded = dbmod.Database(f"sqlite:///{path}")
    upgraded.migrate()
    upgraded.migrate()
    assert "session" in columns()
    assert sqlite3.connect(path).execute("pragma user_version").fetchone() == (2,)


def test_a_page_of_unclosed_headings_is_searched_in_linear_time(db):
    import time

    from artifacts import comments

    body = "<h2 " * 40_000 + "<h2>Shutdown</h2><p>Retries are capped</p>"
    # The best of three: a loaded CI runner once stalled one call for 5.8 s,
    # while the search itself takes about 0.1 s here and quadratic would take minutes.
    times = []
    for _ in range(3):
        started = time.monotonic()
        comments.resolve_text_anchor(body, "Retries are capped", "Shutdown")
        times.append(time.monotonic() - started)
    assert min(times) < 1.0


# --- review fixes, 22 Oct 2026 -------------------------------------------------


def test_on_a_shared_key_another_offices_comment_is_foreign(client, db):
    from artifacts import decide

    token, _ = keys.mint(db, agent="shared")
    headers = {"Authorization": f"Bearer {token}"}
    shared = store.publish(db, "shared", "Board", PAGE, session="harland/a")

    def say(session):
        return client.post(
            f"/api/v1/artifacts/{shared.id}/comments",
            json={"body": "hello"},
            headers={**headers, "X-Artifacts-Session": session},
        )

    assert say("harland/b").status_code == 201  # its own office: no event
    assert say("ch/c").status_code == 201
    heard = [
        e
        for e in events.list_for_agent(db, "shared", exclude_actor="shared")
        if e.kind == "comment_added"
    ]
    assert len(heard) == 1
    mine = {
        "id": "1",
        "author_kind": "agent",
        "author": "shared",
        "session": "harland/b",
        "created_at": "2026-10-22T00:00:00Z",
        "body": "x",
    }
    theirs = {**mine, "id": "2", "thread_root": "1", "session": "ch/c"}
    assert decide._threads([mine], "shared", "harland")[0]["needs_reply"] is False
    assert decide._threads([mine, theirs], "shared", "harland")[0]["needs_reply"]


def test_a_quote_after_a_large_stylesheet_is_found_and_stays_current(client, db, seats):
    body = (
        "<!doctype html><html><body><style>"
        + "a{color:red}" * 10_000
        + "</style><p>The rare passage sits here.</p></body></html>"
    )
    big = store.publish(db, "owner", "Big", body)
    got = _post(
        client,
        seats,
        big.id,
        anchor={"kind": "text", "quote": "the rare passage sits here"},
    )
    assert got.status_code == 201
    [row] = comments.list_for_artifact(db, big.id)
    assert row.quote_state == "current"
    huge = store.publish(
        db, "owner", "Huge", "<!doctype html><p>" + "word " * 500_000 + "</p>"
    )
    refused = _post(
        client, seats, huge.id, anchor={"kind": "text", "quote": "word word"}
    )
    assert refused.status_code == 422
    assert "too much visible text" in refused.json()["detail"]["detail"]


def test_section_is_capped_and_a_heading_is_found_under_its_own_section(
    client, db, seats, page
):
    long = _post(
        client,
        seats,
        page.id,
        anchor={"kind": "text", "quote": "Shutdown", "section": "x" * 201},
    )
    assert long.status_code == 422
    own = _post(
        client,
        seats,
        page.id,
        anchor={"kind": "text", "quote": "Shutdown", "section": "Shutdown"},
    )
    assert own.status_code == 201


def test_a_non_owner_sees_his_comments_and_replies_under_them(client, db, seats, page):
    his = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="his question",
    )
    answer = comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="owner",
        agent="owner",
        body="the owner's answer",
        thread_root=his.id,
    )
    theirs = _post(client, seats, page.id, who="owner").json()
    listed = client.get(
        f"/api/v1/artifacts/{page.id}/comments", headers=seats["other"]
    ).json()
    assert [r["id"] for r in listed] == [his.id, answer.id, theirs["id"]]
    under_his = _post(client, seats, page.id, thread_root=his.id)
    assert under_his.status_code == 201
    assert under_his.json()["author_kind"] == "agent"
    assert under_his.json()["thread_root"] == his.id
    owner_view = client.get(
        f"/api/v1/artifacts/{page.id}/comments", headers=seats["owner"]
    ).json()
    assert under_his.json()["id"] in [r["id"] for r in owner_view]
    unknown = _post(client, seats, page.id, thread_root="nope")
    assert unknown.status_code == 422


def test_a_page_over_the_input_limit_refuses_a_text_anchor(monkeypatch):
    from artifacts import text

    monkeypatch.setattr(text, "INPUT_LIMIT", 100)
    with pytest.raises(comments.TextAnchorError):
        comments.resolve_text_anchor("<p>" + "x" * 200 + "</p>", "x")


def test_the_pages_office_is_its_latest_versions(client, db):
    token, _ = keys.mint(db, agent="shared")
    headers = {"Authorization": f"Bearer {token}"}
    page = store.publish(db, "shared", "Board", PAGE, session="a/1")
    store.patch(
        db,
        "shared",
        page.id,
        PAGE.replace("Retries", "Attempts"),
        store.get_owned(db, "shared", page.id).revision,
        session="b/2",
    )

    def say(session):
        client.post(
            f"/api/v1/artifacts/{page.id}/comments",
            json={"body": "hi"},
            headers={**headers, "X-Artifacts-Session": session},
        )

    say("a/1")  # A comments on what B now owns: B is told
    say("b/9")  # B's own office: nobody is told
    told = [e for e in events.list_for_agent(db, "shared") if e.kind == "comment_added"]
    assert len(told) == 1 and told[0].session == "b/2"


def test_an_owners_own_comment_never_raises_an_event_whatever_its_label(db):
    page = store.publish(db, "Shared", "Board", PAGE, session="ch/1")
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="Shared",
        agent="Shared",
        body="mine",
        session=" SHARED/x",
    )
    assert comments.is_foreign("a", None, "a", None) is False
    assert comments.is_foreign("a", "CH/1", "a", " ch/2") is False
    assert comments.is_foreign("a", "ch/1", "a", "harland/2") is True
    assert [
        e for e in events.list_for_agent(db, "Shared") if e.kind == "comment_added"
    ] == []


def test_a_cut_page_has_an_unknown_quote_state(monkeypatch):
    from artifacts import text

    monkeypatch.setattr(text, "VISIBLE_LIMIT", 10)
    monkeypatch.setattr(text, "SEARCH_LIMIT", 5)
    body = "<p>" + "word " * 100 + "</p>"
    assert comments.quote_state("word word", body) == ""


def test_the_second_scan_waits_for_the_commit(client, db, seats, page, monkeypatch):
    ran = []
    monkeypatch.setattr(comments, "second_scan", lambda d, i: ran.append(i))
    got = _post(client, seats, page.id)
    assert got.status_code == 201 and ran == [got.json()["id"]]


def test_a_non_owner_never_sees_or_answers_a_held_mark(client, db, seats, page):
    decisions.ask_review(db, agent="owner", artifact_id=page.id, mode="held")
    held = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        kind="remove",
        anchor={"kind": "range", "path": "/p[1]", "start": 0, "end": 6},
        quote="The un",
        body="",
    )
    assert held.held
    seen = client.get(
        f"/api/v1/artifacts/{page.id}/comments", headers=seats["other"]
    ).json()
    assert held.id not in [r["id"] for r in seen]
    under = _post(client, seats, page.id, thread_root=held.id)
    unknown = _post(client, seats, page.id, thread_root="nope")
    assert under.status_code == unknown.status_code == 422
    assert under.json() == unknown.json()
