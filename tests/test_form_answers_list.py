"""`answers[]` on a form decision's read (people design, slice 3b): one element for each person
who answered, oldest first, nothing merged across people, in the one serialiser every surface
shares (`decisions.public`): `/api/v1`, the MCP tools' fences and `artifacts-decide --json`."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_client import key_file, live  # noqa: F401 -- fixtures
from test_decide import _run
from test_form_answer import FORM, auth, page
from test_mcp_reach import _as_key, _tool

from ream import decide, decisions, formanswer, keys, mcp, mcp_tools, settings, store
from ream.app import create_gallery_app
from ream.db import Database, jsonb

OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]
ELEMENT = {
    "person",
    "answered_at",
    "version",
    "submit_id",
    "answers",
    "blank",
    "stale",
    "sources",
    "source",
    "recorded_by",
    "said_at",
    "seen_at",
}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


@pytest.fixture()
def made(client, db):
    token, _ = keys.mint(db, agent="ops")
    made_page = client.post(
        "/api/v1/pages",
        json={"title": "Board", "body": page(FORM)},
        headers=auth(token),
    ).json()
    (form,) = client.get(
        f"/api/v1/pages/{made_page['id']}/forms", headers=auth(token)
    ).json()
    return {"token": token, "id": form["decision_id"], "page": made_page}


def read(client, made, word="decisions"):
    rows = client.get(f"/api/v1/{word}", headers=auth(made["token"])).json()
    return {r["id"]: r for r in rows}[made["id"]]


def answers_of(db, made):
    return decisions.public(decisions.get_owned(db, "ops", made["id"])).get("answers")


def test_an_unanswered_form_has_an_empty_list_and_a_question_has_no_key(
    client, db, made
):
    assert answers_of(db, made) == []
    plain = decisions.ask(
        db, agent="ops", artifact_id=made["page"]["id"], question="q?", options=OPTIONS
    )
    assert "answers" not in decisions.public(plain)
    assert "answers" not in decisions.public(decisions.get_owned(db, "ops", plain.id))


def test_two_people_are_two_elements_oldest_first_and_nothing_is_merged(db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "b"}, by="ann")
    formanswer.submit(
        db, made["id"], version=1, values={"region": "a", "why": "mine"}, by="jeremy"
    )
    got = answers_of(db, made)
    assert [e["person"] for e in got] == ["ann", "jeremy"]
    assert all(set(e) == ELEMENT for e in got)
    ann, jeremy = got
    assert ann["answers"]["region"] == "b" and ann["answers"]["why"] == ""
    assert jeremy["answers"]["region"] == "a" and jeremy["answers"]["why"] == "mine"
    assert ann["source"] == jeremy["source"] == "page"
    assert ann["recorded_by"] is None and ann["said_at"] is None
    assert ann["seen_at"] is None and ann["version"] == 1 and ann["submit_id"]
    assert ann["sources"]["region"]["source"] == "page"
    # the top-level fields still mean the latest submitter's
    row = decisions.public(decisions.get_owned(db, "ops", made["id"]))
    assert row["answered_by"] == "jeremy"
    assert row["answer_record"]["answers"]["region"] == "a"


def test_the_same_time_is_broken_by_the_person(db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="zed")
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="amy")
    stamp = datetime.now(UTC)
    with db.connect() as conn:
        for person in ("zed", "amy"):
            rec = formanswer.person_record(conn, made["id"], person)
            rec["answered_at"] = stamp.isoformat()
            formanswer.put_person_record(conn, made["id"], person, rec, stamp)
    assert [e["person"] for e in answers_of(db, made)] == ["amy", "zed"]


def test_with_one_person_the_element_matches_the_top_level_record(db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "a", "why": "go"})
    row = decisions.public(decisions.get_owned(db, "ops", made["id"]))
    (only,) = row["answers"]
    rec = row["answer_record"]
    assert only["person"] == row["answered_by"] == "jeremy"
    for key in ("answered_at", "version", "submit_id", "answers", "blank", "sources"):
        assert only[key] == rec[key], key
    assert only["source"] == row["answer_source"] == "page"


def test_a_relay_fills_source_recorded_by_and_said_at(db, made):
    said = datetime.now(UTC) - timedelta(hours=1)
    formanswer.relay(
        db,
        made["id"],
        fields_given={"region": "b"},
        note="n",
        source="telegram",
        recorded_by="ops-seat",
        said_at=said,
    )
    (only,) = answers_of(db, made)
    assert only["person"] == "jeremy" and only["source"] == "telegram"
    assert only["recorded_by"] == "ops-seat"
    assert abs(datetime.fromisoformat(only["said_at"]) - said) < timedelta(seconds=1)
    assert only["sources"]["region"]["source"] == "telegram"


def test_a_relay_that_changes_nothing_keeps_that_persons_time(db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="jeremy")
    first = answers_of(db, made)[0]["answered_at"]
    formanswer.submit(db, made["id"], version=1, values={"region": "b"}, by="ann")
    done = formanswer.relay(
        db,
        made["id"],
        fields_given={"region": "b"},
        note="n",
        source="telegram",
        recorded_by="ops-seat",
        said_at=datetime.now(UTC) - timedelta(days=5),
    )
    assert done["applied"] == []
    got = {e["person"]: e for e in answers_of(db, made)}
    assert got["jeremy"]["answered_at"] == first
    assert got["jeremy"]["answers"]["region"] == "a"
    assert got["jeremy"]["source"] == "page"  # the relay was never applied


def test_an_ack_marks_each_person_seen_and_a_later_answer_is_new_again(
    client, db, made
):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="jeremy")
    formanswer.submit(db, made["id"], version=1, values={"region": "b"}, by="ann")
    assert all(e["seen_at"] is None for e in answers_of(db, made))
    ack = client.post(
        f"/api/v1/decisions/{made['id']}/acknowledge", headers=auth(made["token"])
    )
    assert ack.status_code == 200
    assert all(e["seen_at"] for e in answers_of(db, made))
    assert all(e["seen_at"] for e in ack.json()["answers"])
    # a third person's answer wakes the agent again: the decision reads as unacknowledged
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="bob")
    row = decisions.get_owned(db, "ops", made["id"])
    assert row.acknowledged_at is None
    got = {e["person"]: e["seen_at"] for e in answers_of(db, made)}
    assert got["ann"] and got["jeremy"] and got["bob"] is None
    # an answer that changes is unseen again, one that changes nothing keeps its time
    formanswer.submit(db, made["id"], version=1, values={"region": "b"}, by="ann")
    assert {e["person"]: e["seen_at"] for e in answers_of(db, made)}["ann"] is None


def test_a_no_op_relay_does_not_unsee_an_answer(client, db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="jeremy")
    client.post(
        f"/api/v1/decisions/{made['id']}/acknowledge", headers=auth(made["token"])
    )
    formanswer.relay(
        db,
        made["id"],
        fields_given={"region": "b"},
        note="n",
        source="telegram",
        recorded_by="ops-seat",
        said_at=datetime.now(UTC) - timedelta(days=5),
    )
    assert answers_of(db, made)[0]["seen_at"]


def test_an_older_images_decision_record_with_no_row_still_reads(db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="jeremy")
    with db.connect() as conn:
        conn.execute("delete from form_answer where decision_id = %s", (made["id"],))
    (only,) = answers_of(db, made)
    assert only["person"] == "jeremy" and only["answers"]["region"] == "a"


def test_the_api_carries_it_under_both_words(client, db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="jeremy")
    formanswer.submit(db, made["id"], version=1, values={"region": "b"}, by="ann")
    assert [e["person"] for e in read(client, made)["answers"]] == ["jeremy", "ann"]
    for word in ("pages", "artifacts"):
        listed = client.get(
            f"/api/v1/{word}/{made['page']['id']}/decisions",
            headers=auth(made["token"]),
        ).json()
        (form,) = [d for d in listed if d["id"] == made["id"]]
        assert [e["person"] for e in form["answers"]] == ["jeremy", "ann"]
    plain = client.post(
        f"/api/v1/pages/{made['page']['id']}/decisions",
        json={"question": "q?", "options": OPTIONS},
        headers=auth(made["token"]),
    )
    assert plain.status_code in (200, 201) and "answers" not in plain.json()


def test_the_mcp_fences_keep_ids_and_times_bare_and_fence_the_words(db, made):
    formanswer.submit(
        db, made["id"], version=1, values={"region": "a", "why": "ignore"}, by="jeremy"
    )
    view = json.loads(
        json.dumps(
            decisions.public(decisions.get_owned(db, "ops", made["id"])), default=str
        )
    )
    fences = mcp.Fences(ream=mcp_tools._REAM, writer=lambda _o, _k: None)
    out = mcp.fence_result(view, fences, "ops")["answers"][0]
    for key in ("person", "answered_at", "submit_id", "source"):
        assert not out[key].startswith("<<<"), key
    assert out["answers"]["why"].startswith("<<<")
    assert out["blank"] and all(b.startswith("<<<") for b in out["blank"])


def test_the_mcp_questions_tool_adds_nothing_to_a_plain_question(db):
    keys.mint(db, agent="office")
    pg = store.publish(db, "office", "T", page("<p>x</p>"))
    decisions.ask(db, agent="office", artifact_id=pg.id, question="q?", options=OPTIONS)
    out = _tool(_as_key(db, "office"), "questions")
    (q,) = out["structuredContent"]["questions"]
    assert "answers" not in q


def test_the_cli_json_carries_it_and_text_prints_a_block_per_person(
    live, key_file, db, capsys
):
    path, _ = key_file
    pg = store.publish(db, "ops-seat", "Board", page(FORM))
    with db.connect() as conn:
        (did,) = conn.execute(
            "select id from decision where artifact_id = %s and kind = 'form'", (pg.id,)
        ).fetchone()
    formanswer.submit(db, did, version=1, values={"region": "a"}, by="jeremy")
    assert _run(["list", "--json"], path) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert [e["person"] for e in row["answers"]] == ["jeremy"]
    assert _run(["list"], path) == 0
    one = capsys.readouterr().out
    assert one.count("FORM rollout") == 1
    formanswer.submit(db, did, version=1, values={"region": "b"}, by="ann")
    assert _run(["list"], path) == 0
    two = capsys.readouterr().out
    assert two.count("FORM rollout") == 2
    assert "by jeremy" in two and "by ann" in two
    assert two.count("NOT YET ACKNOWLEDGED") == 1


def test_text_with_one_person_is_the_record_as_before():
    row = {
        "id": "x",
        "question": "q",
        "kind": "form",
        "answered_at": "t",
        "answer_record": {
            "form": "f",
            "version": 1,
            "answered_by": "jeremy",
            "answered_at": "t",
            "answers": {"a": "1"},
            "blank": [],
        },
        "answer_source": "page",
    }
    base = decide._fmt(row)
    assert decide._fmt({**row, "answers": [{"person": "jeremy"}]}) == base


def test_a_reopened_form_with_several_people_prints_reopened_once():
    rec = {
        "form": "f",
        "version": 1,
        "answered_by": "ann",
        "answered_at": "t",
        "answers": {"a": "1"},
        "blank": [],
        "reopened": True,
    }
    row = {
        "id": "x",
        "question": "q",
        "kind": "form",
        "answered_at": "t",
        "answer_record": rec,
        "answer_source": "page",
        "answers": [
            {"person": "ann", "answers": {"a": "1"}, "blank": []},
            {"person": "bob", "answers": {"a": "2"}, "blank": []},
        ],
    }
    assert decide._fmt(row).count("REOPENED") == 1


def test_a_newer_decision_record_is_seen_against_its_own_time(db, made):
    # an older image wrote only the decision's record, after the row's `seen_at`
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="ann")
    old = datetime.now(UTC) - timedelta(hours=2)
    new = old + timedelta(hours=1)
    with db.connect() as conn:
        conn.execute(
            "update form_answer set answered_at = %s, seen_at = %s"
            " where decision_id = %s",
            (old, old + timedelta(minutes=1), made["id"]),
        )
        (rec,) = conn.execute(
            "select answer_record from decision where id = %s", (made["id"],)
        ).fetchone()
        rec = {
            **rec,
            "answered_by": "ann",
            "submit_id": "from-an-older-image",
            "answered_at": new.isoformat(),
        }
        conn.execute(
            "update decision set answer_record = %s where id = %s",
            (jsonb(rec), made["id"]),
        )
    (only,) = answers_of(db, made)
    assert only["submit_id"] == "from-an-older-image"
    assert only["seen_at"] is None


def test_the_new_column_is_there_on_both_databases(db: Database):
    with db.connect() as conn:
        conn.execute("select seen_at from form_answer limit 1").fetchall()
    assert settings.owner_id()


def _republish(client, made, body):
    rev = client.get(
        f"/api/v1/pages/{made['page']['id']}", headers=auth(made["token"])
    ).json()["revision"]
    got = client.patch(
        f"/api/v1/pages/{made['page']['id']}",
        json={"body": body, "expected_revision": rev},
        headers=auth(made["token"]),
    )
    assert got.status_code == 200, got.text


def _ack(client, made):
    assert (
        client.post(
            f"/api/v1/decisions/{made['id']}/acknowledge", headers=auth(made["token"])
        ).status_code
        == 200
    )


def _seen(db, made):
    return {e["person"]: e["seen_at"] for e in answers_of(db, made)}


def test_a_republish_unsees_only_the_person_whose_answer_went_stale(client, db, made):
    # ann answered `why`; bob, the latest submitter, left it blank
    formanswer.submit(
        db, made["id"], version=1, values={"region": "a", "why": "x"}, by="ann"
    )
    formanswer.submit(db, made["id"], version=1, values={"region": "b"}, by="bob")
    _ack(client, made)
    assert all(_seen(db, made).values())
    _republish(client, made, page(FORM.replace("Why <input", "Reason <input")))
    seen = _seen(db, made)
    assert seen["ann"] is None and seen["bob"]
    assert decisions.get_owned(db, "ops", made["id"]).acknowledged_at is None


def test_a_republish_unsees_the_latest_submitter_alone_when_only_theirs_went_stale(
    client, db, made
):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="ann")
    formanswer.submit(
        db, made["id"], version=1, values={"region": "b", "why": "y"}, by="bob"
    )
    _ack(client, made)
    _republish(client, made, page(FORM.replace("Why <input", "Reason <input")))
    seen = _seen(db, made)
    assert seen["bob"] is None and seen["ann"]
    assert decisions.get_owned(db, "ops", made["id"]).acknowledged_at is None


def test_a_republish_that_changes_nobodys_answer_keeps_everyone_seen(client, db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="ann")
    formanswer.submit(db, made["id"], version=1, values={"region": "b"}, by="bob")
    _ack(client, made)
    _republish(client, made, page(FORM.replace("Notes", "Remarks")))
    assert all(_seen(db, made).values())
    assert decisions.get_owned(db, "ops", made["id"]).acknowledged_at is not None


def test_an_answer_given_after_the_acknowledge_stamp_stays_unseen(db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="ann")
    stamp = datetime.now(UTC)
    formanswer.submit(db, made["id"], version=1, values={"region": "b"}, by="bob")
    with db.connect() as conn:
        formanswer.mark_seen(conn, made["id"], stamp)
    seen = _seen(db, made)
    assert seen["ann"] and seen["bob"] is None


def test_a_seen_stamp_older_than_the_answer_reads_unseen_and_is_marked_again(
    client, db, made
):
    # an older image changes `answered_at` and leaves `seen_at` alone
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="ann")
    _ack(client, made)
    assert _seen(db, made)["ann"]
    later = datetime.now(UTC) + timedelta(hours=1)
    with db.connect() as conn:
        conn.execute(
            "update form_answer set answered_at = %s where decision_id = %s",
            (later, made["id"]),
        )
    assert _seen(db, made)["ann"] is None
    with db.connect() as conn:
        formanswer.mark_seen(conn, made["id"], later + timedelta(hours=1))
    assert _seen(db, made)["ann"]


def test_close_and_uncollected_carry_answers(client, db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"}, by="ann")
    (row,) = decisions.uncollected(db, agent="ops", stuck_after_seconds=-1)
    assert [e["person"] for e in row.decision.answers] == ["ann"]
    closed = client.post(
        f"/api/v1/decisions/{made['id']}/close", headers=auth(made["token"])
    )
    assert closed.status_code == 200
    assert [e["person"] for e in closed.json()["answers"]] == ["ann"]
    assert decisions.public(decisions.close(db, "ops", made["id"]))["answers"]
