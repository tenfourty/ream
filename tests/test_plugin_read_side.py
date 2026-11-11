"""Plug-in API 1.3 (core-and-plugins design, step 9e-2): `api.read_side`, a plug-in's
contribution to the copies, the count, the bridge state and the outbox report that the
inbox, gallery, question list and listening state read. A fake plug-in stands in; core's
own partner code is the built-in contributor and keeps answering first."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import (
    decisions,
    grants,
    inbox,
    keys,
    listening,
    partner_in,
    plugin_api,
    readside,
    sinks,
    store,
)
from ream.app import create_app
from ream.plugin_api import PluginError

PAGE = "<!doctype html><html><head><title>t</title></head><body>hi</body></html>"
HIM = {"Remote-User": "jeremy"}
SAME = {"sec-fetch-site": "same-origin"}

pytestmark = pytest.mark.plugin_loader


class EP:
    def __init__(self, name, register):
        self.name = name
        self._register = register

    def load(self):
        return self._register


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setattr(plugin_api, "_failures", {})
    monkeypatch.setattr(plugin_api, "_loaded", [])
    monkeypatch.setattr(readside, "_CONTRIBUTORS", [])

    def install(*eps):
        monkeypatch.setattr(plugin_api, "_entry_points", lambda: list(eps))
        plugin_api.load()

    with sinks.isolated():
        yield install


def copy(source="qd:x1", title="Open the port?", *, minutes=0, pending=None, **kw):
    fields = dict(  # noqa: C408 -- keyword form reads best here
        source=source,
        version=1,
        asking_office="warpgate",
        title=title,
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended=None,
        context="",
        link=None,
        topic=None,
        depends_on=[],
        replaces=None,
        replaced_by=None,
        urgency="when_you_can",
        blocked_on=None,
        needed_by=None,
        for_offices=[],
        state="open",
        answer=None,
        answer_note="",
        answered_by=None,
        answered_at=None,
        pending_answer=pending,
        pending_note="",
        pending_at=None,
        created_at=datetime(2026, 11, 1, tzinfo=UTC) + timedelta(minutes=minutes),
    )
    fields.update(kw)
    return partner_in.Copy(**fields)


def plugin(**given):
    def register(api):
        api.read_side(**given)

    return EP("chatty", register)


@pytest.fixture()
def gallery(db, monkeypatch):
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://gallery.invalid")
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "content.invalid")
    monkeypatch.setenv("REAM_API_HOST", "api.invalid")
    return TestClient(create_app(db), base_url="https://gallery.invalid")


def test_the_api_version_is_1_3_and_the_cli_api_accepts_the_call():
    from ream import plugin_cli

    assert plugin_api.API_VERSION == (1, 3)
    assert (
        plugin_cli.CliPluginAPI("x").read_side(open_copies=lambda *a, **k: []) is None
    )


def test_core_only_nothing_changes(world, db):
    world()
    assert readside.open_copies(db) == []
    assert readside.count_waiting(db) == 0
    assert readside.outbox_statuses(db) == {}
    assert readside.bridge_on() is False
    assert inbox.count(db) == (0, False)
    assert plugin_api.failure_counts() == {}


def test_a_plugin_copy_reaches_inbox_count_and_listing_in_order(world, db):
    seen = {}

    def opened(db_, limit, waiting_on_him=True):
        seen["args"] = (limit, waiting_on_him)
        return [copy("qd:late", minutes=5), copy("qd:early", minutes=1)]

    world(plugin(open_copies=opened, count_waiting=lambda db_: 2))
    got = readside.open_copies(db, 10)
    assert [c.source for c in got] == ["qd:early", "qd:late"]
    assert seen["args"] == (10, True)
    assert [i.id for i in inbox.items(db)] == ["qd:early", "qd:late"]
    assert inbox.count(db) == (2, False)


def test_a_copy_core_already_holds_or_a_repeat_is_listed_once(world, db):
    def opened(db_, limit, waiting_on_him=True):
        return [copy("qd:a"), copy("qd:a", "again"), copy("qd:b", minutes=1)]

    world(plugin(open_copies=opened))
    assert [c.source for c in readside.open_copies(db)] == ["qd:a", "qd:b"]
    assert readside.open_copies(db, 1)[0].title == "Open the port?"
    assert len(readside.open_copies(db, 1)) == 1


def test_the_gallery_inbox_shows_open_pending_and_settled_copies(world, db, gallery):
    def opened(db_, limit, waiting_on_him=True):
        return (
            [copy("qd:p", "Pending one", pending="a")]
            if not waiting_on_him
            else [copy("qd:w", "Waiting one")]
        )

    def settled(db_, since, limit):
        return [
            copy(
                "qd:s",
                "Settled one",
                state="answered",
                answer="b",
                answered_at="2026-11-02T00:00:00+00:00",
            )
        ]

    world(plugin(open_copies=opened, settled_copies=settled))
    html = gallery.get("/inbox", headers=HIM).text
    assert "Waiting one" in html and "Pending one" in html and "Settled one" in html


def test_the_question_list_scope_all_includes_a_plugin_copy_read_only(
    world, db, gallery
):
    world(
        plugin(
            open_copies=lambda *a, **k: (
                [copy("qd:q9")] if k.get("waiting_on_him", True) else []
            )
        )
    )
    reader, _ = keys.mint(db, agent="reader")
    plain, _ = keys.mint(db, agent="plain")
    grants.grant(db, "reader", grants.READ_ALL_DECISIONS)
    rows = gallery.get(
        "/api/v1/questions?scope=all", headers={"Authorization": f"Bearer {reader}"}
    ).json()
    mine = [r for r in rows if r.get("source") == "qd:q9"]
    assert (
        len(mine) == 1 and mine[0]["read_only"] is True and mine[0]["home"] == "partner"
    )
    # Scoping is unchanged: not for a key without the grant, nor in the default scope.
    assert (
        gallery.get(
            "/api/v1/questions?scope=all", headers={"Authorization": f"Bearer {plain}"}
        ).status_code
        == 403
    )
    own = gallery.get(
        "/api/v1/questions", headers={"Authorization": f"Bearer {reader}"}
    ).json()
    assert all(r.get("source") is None for r in own)


def test_answering_goes_to_the_plugin_for_a_question_core_does_not_hold(
    world, db, gallery
):
    calls = []

    def answer(db_, qid, *, letter, note=""):
        calls.append((qid, letter, note))
        if qid != "x1":
            raise store.NotFound()
        if letter not in "ab":
            raise decisions.InvalidAnswer()
        return copy("qd:x1", pending=letter)

    world(plugin(answer=answer))
    ok = gallery.post(
        "/pq/x1/answer",
        data={"letter": "a", "note": "n"},
        headers=SAME | HIM,
        follow_redirects=False,
    )
    assert ok.status_code == 303 and calls[-1] == ("x1", "a", "n")
    bad = gallery.post("/pq/x1/answer", data={"letter": "z"}, headers=SAME | HIM)
    assert bad.status_code == 422
    other = gallery.post("/pq/zz/answer", data={"letter": "a"}, headers=SAME | HIM)
    assert other.status_code == 404


def test_bridge_state_is_core_or_any_plugin(world, db):
    world(plugin(enabled=lambda: True))
    assert readside.bridge_on() is True
    assert listening.status(db, "seat-a")["bridge"] == {"enabled": True}


def test_the_outbox_status_is_collected_by_plugin_and_reaches_the_report(world, db):
    world(plugin(outbox_status=lambda db_: {"depth": 3, "parked": 1}))
    assert readside.outbox_statuses(db) == {"chatty": {"depth": 3, "parked": 1}}
    assert sinks.status(db)["read_side_outbox"] == {"chatty": {"depth": 3, "parked": 1}}


def test_a_contributor_that_raises_is_isolated_logged_and_counted(
    world, db, gallery, caplog
):
    def boom(*a, **k):
        raise RuntimeError("secret detail")

    world(
        plugin(
            open_copies=boom,
            settled_copies=boom,
            count_waiting=boom,
            enabled=boom,
            outbox_status=boom,
        )
    )
    assert readside.open_copies(db) == []
    assert readside.count_waiting(db) == 0
    assert readside.bridge_on() is False
    assert readside.outbox_statuses(db) == {}
    assert gallery.get("/inbox", headers=HIM).status_code == 200
    assert inbox.count(db) == (0, False)
    assert plugin_api.failure_counts()["chatty:read_side"] >= 5
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text


def test_wrong_shapes_are_dropped_and_counted(world, db):
    world(
        plugin(
            open_copies=lambda *a, **k: [copy("qd:ok"), {"source": "x"}, object()],
            count_waiting=lambda db_: -4,
            outbox_status=lambda db_: ["not", "a", "dict"],
        )
    )
    assert [c.source for c in readside.open_copies(db)] == ["qd:ok"]
    assert readside.count_waiting(db) == 0
    assert readside.outbox_statuses(db) == {}
    assert plugin_api.failure_counts()["chatty:read_side"] == 3


def test_a_plugin_that_fails_answering_does_not_hide_the_next_one(world, db):
    def boom(db_, qid, *, letter, note=""):
        raise RuntimeError

    def good(db_, qid, *, letter, note=""):
        return copy("qd:" + qid, pending=letter)

    def other(api):
        api.read_side(answer=good)

    world(plugin(answer=boom), EP("zeta", other))
    assert readside.answer_here(db, "q1", letter="a").pending_answer == "a"
    assert plugin_api.failure_counts()["chatty:read_side"] == 1


def test_registration_refusals(world):
    with pytest.raises(PluginError, match="gives nothing"):
        world(plugin())
    with pytest.raises(PluginError, match="not callable"):
        world(plugin(open_copies="no"))

    def twice(api):
        api.read_side(enabled=lambda: True)
        api.read_side(enabled=lambda: True)

    with pytest.raises(PluginError, match="twice"):
        world(EP("dup", twice))


@pytest.mark.parametrize(
    ("note", "refusal"),
    [
        ("x" * (decisions.MAX_NOTE_BYTES + 1), store.BodyTooLarge),
        ("key AKIA" + "IOSFODNN7EXAMPLE here", store.SecretFound),
    ],
)
def test_a_note_is_gated_before_a_plugin_ever_sees_it(
    world, db, gallery, note, refusal
):
    calls = []

    def answer(db_, qid, *, letter, note=""):
        calls.append(note)
        return copy("qd:x1", pending=letter)

    world(plugin(answer=answer))
    with pytest.raises(refusal):
        readside.answer_here(db, "x1", letter="a", note=note)
    sent = gallery.post(
        "/pq/x1/answer", data={"letter": "a", "note": note}, headers=SAME | HIM
    )
    assert sent.status_code in (413, 422)
    assert calls == []
