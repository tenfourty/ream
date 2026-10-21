"""A retried reply or acknowledgement never posts twice (page review step 5)."""

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_client import key_file, live  # noqa: F401

from artifacts import comments, decide, decisions, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def client(db):
    return TestClient(create_gallery_app(db))


def _auth(db, agent="seat"):
    token, _ = keys.mint(db, agent=agent)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def auth(db):
    return _auth(db)


@pytest.fixture()
def page(db):
    return store.publish(db, "seat", "Board", PAGE)


def _post(client, auth, page, body="hello", key=None):
    headers = dict(auth)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(
        f"/api/v1/artifacts/{page.id}/comments", json={"body": body}, headers=headers
    )


def _count(db, page):
    with db.connect() as conn:
        return conn.execute(
            "select count(*) from comment where artifact_id = %s", (page.id,)
        ).fetchone()[0]


def test_a_repeat_returns_the_first_response_and_writes_nothing(client, auth, db, page):
    first = _post(client, auth, page, key="k1")
    again = _post(client, auth, page, key="k1")
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    assert _count(db, page) == 1


def test_a_repeat_runs_no_second_scan(client, auth, page, monkeypatch):
    scans = []
    monkeypatch.setattr(comments, "second_scan", lambda db, cid: scans.append(cid))
    _post(client, auth, page, key="k1")
    _post(client, auth, page, key="k1")
    assert len(scans) == 1


def test_no_key_means_no_deduplication(client, auth, db, page):
    _post(client, auth, page)
    _post(client, auth, page)
    assert _count(db, page) == 2


def test_the_same_key_with_a_different_body_is_refused(client, auth, db, page):
    _post(client, auth, page, body="one", key="k1")
    r = _post(client, auth, page, body="two", key="k1")
    assert r.status_code == 422
    assert r.json()["detail"] == "idempotency key reused with a different request"
    assert _count(db, page) == 1


def test_another_agents_identical_key_is_independent(client, auth, db, page):
    other = _auth(db, "other")
    mine = _post(client, auth, page, key="k1")
    theirs = _post(client, other, page, key="k1")
    assert mine.status_code == 201
    # An open page takes their comment too, as its own write, not mine replayed.
    assert theirs.status_code == 201 and theirs.json()["id"] != mine.json()["id"]
    assert _count(db, page) == 2


@pytest.mark.parametrize(
    "key,ok",
    [("a" * 128, True), ("a" * 129, False), ("", False), ("tab\there", False)],
)
def test_the_key_is_128_printable_ascii(client, auth, page, key, ok):
    r = _post(client, auth, page, key=key)
    assert (r.status_code == 201) is ok
    if not ok:
        assert r.status_code == 422


def test_a_refusal_is_not_remembered(client, auth, page):
    bad = _post(client, auth, page, body="AKIAIOSFODNN7EXAMPLE " * 3, key="k1")
    assert bad.status_code in (422, 503)
    assert _post(client, auth, page, body="fine", key="k1").status_code == 201


def test_expired_rows_are_pruned_and_no_longer_replay(client, auth, db, page):
    _post(client, auth, page, key="old")
    with db.connect() as conn:
        conn.execute(
            "update idempotency set expires_at = %s",
            (datetime.now(UTC) - timedelta(seconds=1),),
        )
    r = _post(client, auth, page, key="new")
    assert r.status_code == 201
    with db.connect() as conn:
        keys_left = [x[0] for x in conn.execute("select key from idempotency")]
    assert keys_left == ["new"]
    # and the expired key runs afresh rather than replaying
    with db.connect() as conn:
        conn.execute(
            "update idempotency set expires_at = %s",
            (datetime.now(UTC) - timedelta(seconds=1),),
        )
    _post(client, auth, page, key="new")
    assert _count(db, page) == 3


def test_a_repeated_acknowledge_replays(client, auth, db, page):
    row = decisions.ask(
        db, agent="seat", artifact_id=page.id, question="q?", options=OPTIONS
    )
    h = {**auth, "Idempotency-Key": "ack1"}
    a = client.post(f"/api/v1/decisions/{row.id}/acknowledge", headers=h)
    b = client.post(f"/api/v1/decisions/{row.id}/acknowledge", headers=h)
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()


def test_two_identical_requests_at_once_make_one_comment(
    client, auth, db, page, monkeypatch
):
    reached, go = threading.Event(), threading.Event()
    real = comments.add
    calls = []

    def gated(*args, **kwargs):
        calls.append(1)
        out = real(*args, **kwargs)
        reached.set()
        assert go.wait(10)
        return out

    monkeypatch.setattr(comments, "add", gated)
    out = {}

    def go_post(name):
        out[name] = _post(client, auth, page, key="race")

    first = threading.Thread(target=go_post, args=("a",))
    first.start()
    assert reached.wait(10)
    second = threading.Thread(target=go_post, args=("b",))
    second.start()
    time.sleep(0.3)  # the second is parked on the key's lock
    go.set()
    first.join(20)
    second.join(20)
    assert out["a"].status_code == out["b"].status_code == 201
    assert out["a"].json() == out["b"].json()
    assert len(calls) == 1
    assert _count(db, page) == 1


# --- the CLI ------------------------------------------------------------------


def _seen_keys(monkeypatch):
    seen = []
    real = decide._request

    def spy(*args, **kwargs):
        if kwargs.get("idempotency_key"):
            seen.append(kwargs["idempotency_key"])
        return real(*args, **kwargs)

    monkeypatch.setattr(decide, "_request", spy)
    return seen


def test_reply_and_ack_send_a_fresh_key_each_invocation_and_never_print_it(
    live,
    key_file,
    db,
    page,
    monkeypatch,
    capsys,
):
    path, _ = key_file
    seen = _seen_keys(monkeypatch)
    mine = store.publish(db, "ops-seat", "Mine", PAGE)
    root = comments.add(
        db, artifact_id=mine.id, author_kind="human", author="jeremy", body="hi"
    )
    row = decisions.ask(
        db, agent="ops-seat", artifact_id=mine.id, question="q?", options=OPTIONS
    )
    args = ["--key-file", str(path)]
    assert decide.main([*args, "reply", root.id, "one"]) == 0
    assert decide.main([*args, "reply", root.id, "two"]) == 0
    assert decide.main([*args, "ack", row.id]) == 0
    out = capsys.readouterr()
    assert len(seen) == 3 and len(set(seen)) == 3
    assert all(k not in out.out + out.err for k in seen)


def test_the_client_retry_reuses_the_same_key(live, key_file, monkeypatch):
    from artifacts import client as client_mod

    _path, token = key_file
    sent = []

    class Boom(Exception):
        pass

    real_open = client_mod._opener.open

    def fake_open(request, timeout=None):
        sent.append(request.get_header("Idempotency-key"))
        if len(sent) == 1:
            import io
            import urllib.error

            raise urllib.error.HTTPError(
                request.full_url, 502, "bad gateway", {}, io.BytesIO(b"x")
            )
        return real_open(request, timeout=timeout)

    monkeypatch.setattr(client_mod._opener, "open", fake_open)
    monkeypatch.setattr(client_mod, "_ROLLING_BACKOFF", 0)
    with pytest.raises(Exception):  # noqa: B017 -- the real call 404s; keys matter
        client_mod._request(
            "POST",
            f"{live}/api/v1/decisions/none/acknowledge",
            token,
            {},
            idempotency_key="fixed",
        )
    assert sent[:2] == ["fixed", "fixed"]


# --- the write and its stored response commit together ------------------------


def _fail_storing(monkeypatch):
    from artifacts import idempotency

    def boom(content):
        raise RuntimeError("crashed before the response was stored")

    monkeypatch.setattr(idempotency, "jsonable_encoder", boom)


def test_a_crash_before_the_response_is_stored_leaves_no_comment(
    client, auth, db, page, monkeypatch
):
    """Otherwise the comment is committed without its stored response, and the
    retry with the same key posts it a second time."""
    _fail_storing(monkeypatch)
    with pytest.raises(RuntimeError):
        _post(client, auth, page, key="k1")
    assert _count(db, page) == 0
    monkeypatch.undo()
    assert _post(client, auth, page, key="k1").status_code == 201
    assert _count(db, page) == 1


def test_a_crash_before_the_response_is_stored_leaves_no_acknowledgement(
    client, auth, db, page, monkeypatch
):
    row = decisions.ask(
        db, agent="seat", artifact_id=page.id, question="q?", options=OPTIONS
    )
    _fail_storing(monkeypatch)
    with pytest.raises(RuntimeError):
        client.post(
            f"/api/v1/decisions/{row.id}/acknowledge",
            headers={**auth, "Idempotency-Key": "ack1"},
        )
    assert decisions.get_owned(db, "seat", row.id).acknowledged_at is None


def test_a_reply_and_an_ack_without_a_key_still_write(client, auth, db, page):
    row = decisions.ask(
        db, agent="seat", artifact_id=page.id, question="q?", options=OPTIONS
    )
    assert _post(client, auth, page).status_code == 201
    r = client.post(f"/api/v1/decisions/{row.id}/acknowledge", headers=auth)
    assert r.status_code == 200 and r.json()["acknowledged_by"] == "seat"


# --- pruning ------------------------------------------------------------------


@pytest.mark.postgres_only  # reads pg_indexes and uses generate_series
def test_expired_rows_are_pruned_by_an_index_a_bounded_batch_at_a_time(
    client, auth, db, page
):
    with db.connect() as conn:
        found = conn.execute(
            "select indexdef from pg_indexes where tablename = 'idempotency'"
            " and indexdef like '%%(expires_at)%%'"
        ).fetchall()
        assert found, "no index on idempotency.expires_at"
        conn.execute(
            "insert into idempotency"
            " (agent, route, key, body_sha256, status, response, expires_at)"
            " select 'seat', 'r', 'old' || g, 'x', 201, '{}',"
            " now() - interval '1 hour' from generate_series(1, 250) g"
        )
    assert _post(client, auth, page, key="new").status_code == 201
    with db.connect() as conn:
        left = conn.execute(
            "select count(*) from idempotency where expires_at < now()"
        ).fetchone()[0]
    assert left == 150


def test_a_comment_that_would_nudge_the_owner_cannot_join_a_callers_transaction(
    db, page
):
    """The nudge fires after the commit; inside a caller's transaction it
    would fire before one, for a row that might still roll back."""
    with db.connect() as conn, conn.transaction(), pytest.raises(ValueError):
        comments.add(
            db,
            conn=conn,
            artifact_id=page.id,
            author_kind="human",
            author="jeremy",
            body="hi",
        )
