"""The parts a round changed, stored, on /changes, in the versions list and over MCP
(runcove-5t3oj.56, slice 2)."""

import pytest
from fastapi.testclient import TestClient

from ream import keys, pagediff, partchanges, store
from ream.app import create_gallery_app


def page(*sections: str) -> str:
    return (
        "<!doctype html><html><head><title>t</title></head><body>"
        + "".join(sections)
        + "</body></html>"
    )


def sec(id_: str, text: str) -> str:
    return f'<section id="{id_}"><h2>{id_.title()}</h2><p>{text}</p></section>'


V1 = page(sec("running", "a"), sec("results", "r"))
V2 = page(sec("running", "b"), sec("results", "r"))
V3 = page(sec("running", "b"), sec("results", "r"), sec("risks", "new"))


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def seat(db):
    token, _ = keys.mint(db, agent="seat")
    made = store.publish(db, "seat", "Board", V1)
    rev = made.revision
    for body in (V2, V3):
        rev = store.patch(db, "seat", made.id, body, rev).revision
    return {"id": made.id, "auth": {"Authorization": f"Bearer {token}"}}


def get(api, seat, path, **params):
    return api.get(
        f"/api/v1/pages/{seat['id']}{path}", headers=seat["auth"], params=params
    )


def test_one_round_names_the_part(api, seat):
    got = get(api, seat, "/changes", **{"from": 1, "to": 2}).json()
    assert got["from"] == 1 and got["to"] == 2
    assert got["parts"] == [{"id": "running", "label": "Running", "change": "changed"}]
    assert got["unknown_reason"] is None and got["more"] == 0


def test_to_defaults_to_the_live_version_and_a_span_is_one_diff(api, seat):
    got = get(api, seat, "/changes", **{"from": 1}).json()
    assert got["to"] == 3
    assert [(p["id"], p["change"]) for p in got["parts"]] == [
        ("running", "changed"),
        ("risks", "added"),
    ]


def test_the_same_version_has_no_changes_and_a_backwards_span_is_refused(api, seat):
    got = get(api, seat, "/changes", **{"from": 2, "to": 2}).json()
    assert got["parts"] == [] and got["unknown_reason"] is None
    assert get(api, seat, "/changes", **{"from": 3, "to": 1}).status_code == 422
    assert get(api, seat, "/changes").status_code == 422  # from is needed


def test_an_unknown_version_or_page_is_the_same_404(api, seat):
    missing_version = get(api, seat, "/changes", **{"from": 9})
    missing_page = api.get(
        "/api/v1/pages/nope/changes", headers=seat["auth"], params={"from": 1}
    )
    assert missing_version.status_code == 404 == missing_page.status_code


def test_a_closed_page_of_another_agent_answers_like_no_page(db, api, seat):
    token, _ = keys.mint(db, agent="other")
    store.set_agents_can_read(db, seat["id"], False, agent="seat")
    got = api.get(
        f"/api/v1/pages/{seat['id']}/changes",
        headers={"Authorization": f"Bearer {token}"},
        params={"from": 1},
    )
    nope = api.get(
        "/api/v1/pages/nope/changes",
        headers={"Authorization": f"Bearer {token}"},
        params={"from": 1},
    )
    assert got.status_code == nope.status_code == 404
    assert got.text == nope.text


def test_marked_returns_the_marked_up_body(api, seat):
    got = get(api, seat, "/changes", **{"from": 1, "to": 2, "format": "marked"}).json()
    assert "ar-diff-ins" in got["body"] and got["reason"] is None
    assert got["parts"][0]["id"] == "running"


def test_a_round_is_stored_and_the_versions_list_carries_it(db, api, seat):
    before = {v["n"]: v for v in get(api, seat, "/versions").json()}
    assert before[2]["part_changes"] is None
    partchanges.record(db, seat["id"], 2)
    partchanges.record(db, seat["id"], 1)  # the first version has no round before it
    after = {v["n"]: v for v in get(api, seat, "/versions").json()}
    assert after[2]["part_changes"]["parts"][0]["id"] == "running"
    assert after[1]["part_changes"] is None
    # served from the stored round, not computed again
    stored = store.stored_part_changes(db, seat["id"], 2)
    assert stored["from"] == 1 and stored["parts"][0]["label"] == "Running"


def test_a_page_it_cannot_read_stores_nothing_and_says_so(db, seat, monkeypatch):
    monkeypatch.setattr(
        partchanges.partdiff,
        "changes_safely",
        lambda *a, **k: {
            "parts": [],
            "more": 0,
            "unknown_reason": "could not tell which parts changed",
        },
    )
    partchanges.record(db, seat["id"], 2)
    # a failure from load says nothing about the pages: not kept, tried again next time
    assert store.stored_part_changes(db, seat["id"], 2) is None


def test_a_page_too_big_to_read_is_kept_as_such(db, seat, monkeypatch):
    monkeypatch.setattr(
        partchanges.partdiff,
        "changes_safely",
        lambda *a, **k: {
            "parts": [],
            "more": 0,
            "unknown_reason": "too large to mark up changes",
        },
    )
    partchanges.record(db, seat["id"], 2)
    assert store.stored_part_changes(db, seat["id"], 2)["unknown_reason"].startswith(
        "too large"
    )


def test_the_publish_route_does_not_wait_for_it(db, api, seat, monkeypatch):
    called = []
    monkeypatch.setattr(
        "ream.api.partchanges.in_background", lambda *a: called.append(a)
    )
    one = get(api, seat, "")  # a read starts nothing
    assert one.status_code == 200 and called == []
    body = api.get(f"/api/v1/pages/{seat['id']}/body", headers=seat["auth"]).json()
    posted = api.patch(
        f"/api/v1/pages/{seat['id']}",
        headers=seat["auth"],
        json={"body": V1, "expected_revision": body["revision"]},
    )
    assert posted.status_code == 200, posted.text
    assert called and called[0][1:] == (seat["id"], 4)


# --- the tool ---------------------------------------------------------------------


def test_the_publish_line_names_two_parts_and_counts_the_rest():
    from ream import client

    parts = [
        {"label": x, "change": "changed"} for x in ("Running now", "Results", "A", "B")
    ]
    assert client._changes_line({"parts": parts, "more": 1}) == (
        "changed: Running now, Results (+3)"
    )
    assert (
        client._changes_line({"parts": parts[:1], "more": 0}) == "changed: Running now"
    )
    assert "could not tell" in client._changes_line(
        {"parts": [], "unknown_reason": "this page draws itself with script"}
    )
    assert client._changes_line({"parts": [], "unknown_reason": None}).startswith(
        "changed: no part"
    )


def test_publish_prints_the_line_and_says_nothing_for_a_first_version(
    monkeypatch, capsys
):
    from ream import client

    monkeypatch.setattr(
        client,
        "_request",
        lambda *a, **k: {"parts": [{"label": "Results", "change": "added"}], "more": 0},
    )
    client._report_changes("abc", 2, "k")
    assert "changed: Results" in capsys.readouterr().err
    client._report_changes("abc", 1, "k")
    assert capsys.readouterr().err == ""


def test_ream_changes_lists_each_part_and_never_exits_clean_on_an_unknown(
    monkeypatch, capsys
):
    from ream import client

    seen = []

    def fake(method, url, key, body):
        seen.append(url)
        if url.endswith("/versions"):
            return [{"n": 3, "live": True}, {"n": 2, "live": False}]
        return {
            "from": 2,
            "to": 3,
            "parts": [
                {"id": "running", "label": "Running \x1b[31m", "change": "changed"},
                {"label": "Old", "change": "removed"},
            ],
            "more": 0,
            "unknown_reason": None,
        }

    monkeypatch.setattr(client, "_request", fake)
    assert client._print_changes("abc", "k", None, None) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["changed\trunning\tRunning [31m", "removed\t\tOld"]
    assert seen[-1].endswith("/changes?from=2")

    monkeypatch.setattr(
        client,
        "_request",
        lambda *a, **k: {
            "parts": [],
            "more": 0,
            "unknown_reason": "too large to mark up changes",
        },
    )
    assert client._print_changes("abc", "k", 1, 3) == 3
    assert "could not tell" in capsys.readouterr().err


def test_ream_changes_hands_its_flags_to_the_publish_tool(monkeypatch):
    import sys

    from ream import client

    seen = []
    monkeypatch.setattr(client, "run", lambda: seen.append(list(sys.argv)))
    monkeypatch.setattr(sys, "argv", ["ream", "abc", "--from", "1", "--to", "3"])
    client.changes_run()
    assert seen == [
        ["ream changes", "--changes", "abc", "--from", "1", "--changes-to", "3"]
    ]


def _answer(reason):
    return {"parts": [], "more": 0, "unknown_reason": reason}


def test_a_diff_that_timed_out_is_not_kept_by_a_read_and_is_asked_again(
    db, api, seat, monkeypatch
):
    calls = []

    def slow(*a, **k):
        calls.append(1)
        return _answer(partchanges.partdiff.REASON_SLOW)

    monkeypatch.setattr("ream.api.partdiff.changes_safely", slow)
    first = get(api, seat, "/changes", **{"from": 1, "to": 2}).json()
    second = get(api, seat, "/changes", **{"from": 1, "to": 2}).json()
    assert (
        first["unknown_reason"]
        == second["unknown_reason"]
        == partchanges.partdiff.REASON_SLOW
    )
    assert len(calls) == 2, "the answer is computed on each read"
    assert store.stored_part_changes(db, seat["id"], 2) is None


def test_the_worker_after_a_publish_still_keeps_a_diff_that_timed_out(
    db, api, seat, monkeypatch
):
    calls = []

    def slow(*a, **k):
        calls.append(1)
        return _answer(partchanges.partdiff.REASON_SLOW)

    monkeypatch.setattr("ream.partchanges.partdiff.changes_safely", slow)
    partchanges.record(db, seat["id"], 2)
    assert store.stored_part_changes(db, seat["id"], 2)["unknown_reason"] == (
        partchanges.partdiff.REASON_SLOW
    )
    got = get(api, seat, "/changes", **{"from": 1, "to": 2}).json()
    assert got["unknown_reason"] == partchanges.partdiff.REASON_SLOW
    assert len(calls) == 1, "a read after the worker kept it did not recompute"


@pytest.mark.parametrize("reason", [None, pagediff.REASON_LARGE])
def test_a_read_still_keeps_a_real_answer_and_one_that_is_too_large(
    db, api, seat, monkeypatch, reason
):
    calls = []

    def answer(*a, **k):
        calls.append(1)
        return _answer(reason)

    monkeypatch.setattr("ream.api.partdiff.changes_safely", answer)
    get(api, seat, "/changes", **{"from": 1, "to": 2})
    get(api, seat, "/changes", **{"from": 1, "to": 2})
    assert len(calls) == 1
    assert store.stored_part_changes(db, seat["id"], 2)["unknown_reason"] == reason


def test_a_worker_timeout_is_its_own_answer_not_a_transient_failure(monkeypatch):
    from ream import pagediff

    class Slow:
        def alive(self):
            return True

        def run(self, *a):
            raise TimeoutError

        def kill(self):
            pass

    monkeypatch.setattr(pagediff, "_Worker", Slow)
    monkeypatch.setattr(pagediff, "_IDLE", [])
    partchanges.partdiff._CACHE.clear()
    got = partchanges.partdiff.changes_safely("<p>a</p>", "<p>b</p>")
    assert got["unknown_reason"] == partchanges.partdiff.REASON_SLOW
    assert partchanges.keepable(got)


def test_two_versions_are_both_rebuilt_when_the_newer_one_is_already_cached(db, seat):
    """A cached newer version must not start the walk above an older one asked for."""
    with db.connect() as conn:
        store.version_bodies(conn, seat["id"], [2])
        got = store.version_bodies(conn, seat["id"], [1, 2])
    assert sorted(got) == [1, 2]
