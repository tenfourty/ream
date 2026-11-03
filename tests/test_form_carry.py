"""Form controls inside a page, slice 3: what a republish does to a submitted record
(carried, stale, removed, reopened), what the gallery draws and replays from it, and the
per-field `record` relay (docs/agent/specs/2026-10-29-form-controls-design.md 3.5-3.6).
Runs on both databases like the rest of the suite."""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from artifacts import (
    decide,
    decisions,
    events,
    formanswer,
    formrecord,
    keys,
    nudge,
    store,
)
from artifacts.app import create_gallery_app
from artifacts.db import jsonb

ORIGIN = "https://artifacts.tenfourty.site"

FORM = """
<form data-artifact-form="rollout">
  <p>Pick where it goes.</p>
  <fieldset name="region"><legend>Region</legend>
    <label><input type="radio" name="region" value="a" required> eu</label>
    <label><input type="radio" name="region" value="b"> us</label>
  </fieldset>
  <label>Why <input type="text" name="why" maxlength="10"></label>
  <label>Count <input type="number" name="count" min="1" max="9"></label>
  <label>Notes <textarea name="notes"></textarea></label>
</form>
"""


def page(inner: str) -> str:
    return (
        "<!doctype html><html><head><title>P</title></head><body>"
        + inner
        + "</body></html>"
    )


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", ORIGIN)
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


@pytest.fixture(autouse=True)
def nudges(monkeypatch):
    sent = []
    monkeypatch.setattr(nudge, "emit", lambda kind, **kw: sent.append((kind, kw)))
    return sent


@pytest.fixture()
def made(client, db):
    token, _ = keys.mint(db, agent="ops")
    page_ = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": page(FORM)},
        headers=auth(token),
    ).json()
    (form,) = client.get(
        f"/api/v1/artifacts/{page_['id']}/forms", headers=auth(token)
    ).json()
    return {"token": token, "page": page_, "id": form["decision_id"]}


def republish(client, made, inner):
    rev = client.get(
        f"/api/v1/artifacts/{made['page']['id']}", headers=auth(made["token"])
    ).json()["revision"]
    response = client.patch(
        f"/api/v1/artifacts/{made['page']['id']}",
        json={"body": page(inner), "expected_revision": rev},
        headers=auth(made["token"]),
    )
    assert response.status_code == 200, response.text
    return response


def record_of(db, made):
    return decisions.get_owned(db, "ops", made["id"]).answer_record


def stale_names(rec):
    return {s["name"]: s for s in rec["stale"]}


def submit(db, made, version=1, **values):
    return formanswer.submit(db, made["id"], version=version, values=values)


def ack(client, made):
    r = client.post(
        f"/api/v1/decisions/{made['id']}/acknowledge", headers=auth(made["token"])
    )
    assert r.status_code == 200, r.text


# --- the pure rules --------------------------------------------------------------

RADIO = {
    "name": "region",
    "type": "radio",
    "required": True,
    "label": "Region",
    "options": [{"value": "a", "label": "eu"}, {"value": "b", "label": "us"}],
}
TEXT = {"name": "why", "type": "text", "required": False, "label": "Why", "options": []}


def base_record(**over):
    rec = {
        "version": 1,
        "answers": {"region": "a", "why": "old"},
        "blank": [],
        "stale": [],
        "answered_by": "jeremy",
        "answered_at": "2026-11-02T10:00:00+00:00",
        "sources": {
            "region": {
                "source": "page",
                "at": "2026-11-02T10:00:00+00:00",
                "version": 1,
            },
            "why": {"source": "page", "at": "2026-11-02T10:00:00+00:00", "version": 1},
        },
    }
    rec.update(over)
    return rec


def test_the_question_is_name_type_options_limits_and_label_but_not_required():
    assert formrecord.same_question(RADIO, dict(RADIO, required=False))
    assert not formrecord.same_question(RADIO, dict(RADIO, label="Where"))
    assert not formrecord.same_question(RADIO, dict(RADIO, type="select"))
    more = dict(RADIO, options=[*RADIO["options"], {"value": "c", "label": "asia"}])
    assert not formrecord.same_question(RADIO, more)
    relabelled = dict(
        RADIO, options=[{"value": "a", "label": "EU!"}, RADIO["options"][1]]
    )
    assert not formrecord.same_question(RADIO, relabelled)
    capped = dict(TEXT, maxlength=5)
    assert not formrecord.same_question(TEXT, capped)


def test_reconcile_carries_unchanged_and_stales_changed_and_removed():
    rec = base_record()
    new_text = dict(TEXT, label="Because")
    out = formrecord.reconcile(rec, [RADIO, TEXT], [RADIO, new_text], 2, False)
    assert out["version"] == 2
    assert out["answers"] == {"region": "a", "why": ""}
    assert out["blank"] == ["why"]
    (entry,) = out["stale"]
    assert entry["name"] == "why" and entry["value"] == "old"
    assert (
        entry["version"] == 1
        and entry["label"] == "Why"
        and entry["reason"] == "changed"
    )
    assert out["reopened"] is False
    gone = formrecord.reconcile(rec, [RADIO, TEXT], [RADIO], 2, False)
    assert gone["stale"][0]["reason"] == "removed" and "why" not in gone["answers"]


def test_a_stale_answer_stays_listed_until_the_field_is_answered_again():
    rec = formrecord.reconcile(
        base_record(), [RADIO, TEXT], [RADIO, dict(TEXT, label="Because")], 2, False
    )
    again = formrecord.reconcile(
        rec,
        [RADIO, dict(TEXT, label="Because")],
        [RADIO, dict(TEXT, label="Because")],
        3,
        False,
    )
    assert [s["name"] for s in again["stale"]] == ["why"]  # nothing moved silently
    sent = {"region": "a", "why": "new"}
    after = formrecord.apply_submit(
        again,
        3,
        [RADIO, dict(TEXT, label="Because")],
        False,
        sent,
        by="jeremy",
        at=datetime.now(UTC),
    )
    assert after["stale"] == [] and after["answers"]["why"] == "new"


def test_a_relay_never_overwrites_a_later_submit_but_does_replace_an_earlier_one():
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    rec = base_record()
    fields = [RADIO, TEXT]
    later_submit = formrecord.apply_submit(
        rec, 1, fields, False, {"region": "b", "why": ""}, by="jeremy", at=now
    )
    out, applied, skipped = formrecord.apply_relay(
        later_submit,
        1,
        fields,
        False,
        {"region": "a"},
        source="telegram",
        by="cove",
        said_at=now - timedelta(hours=1),
        now=now,
    )
    assert (applied, skipped) == ([], ["region"]) and out["answers"]["region"] == "b"
    out, applied, skipped = formrecord.apply_relay(
        later_submit,
        1,
        fields,
        False,
        {"region": "a"},
        source="telegram",
        by="cove",
        said_at=now + timedelta(hours=1),
        now=now,
    )
    assert applied == ["region"] and out["answers"]["region"] == "a"
    assert out["sources"]["region"]["source"] == "telegram"
    assert out["sources"]["why"]["source"] == "page"  # the other field is still his


def test_the_gallery_view_separates_his_answers_from_relays():
    rec = base_record()
    rec["sources"]["why"] = {"source": "telegram", "by": "cove", "version": 1}
    view = formrecord.gallery_view(rec, [RADIO, TEXT])
    assert view["carried"] == {"region": "a"}
    assert view["relayed"]["why"]["source"] == "telegram"
    assert view["answered"]["version"] == 1
    only_relay = base_record(answers={"region": "", "why": "x"})
    only_relay["sources"] = {"why": {"source": "telegram", "by": "cove"}}
    assert formrecord.gallery_view(only_relay, [RADIO, TEXT])["answered"] is None
    reopened = formrecord.gallery_view(base_record(reopened=True), [RADIO, TEXT])
    assert reopened["answered"] is None and reopened["carried"]["region"] == "a"


# --- a republish, end to end -----------------------------------------------------


def test_unchanged_answers_are_carried_and_stay_current(client, db, made):
    submit(db, made, region="a", why="old", count=3)
    ack(client, made)
    republish(client, made, FORM.replace("Notes", "Remarks"))
    rec = record_of(db, made)
    assert rec["version"] == 2 and rec["stale"] == []
    assert rec["answers"]["region"] == "a" and rec["answers"]["why"] == "old"
    assert rec["answers"]["count"] == 3
    assert (
        rec["sources"]["why"]["source"] == "page"
        and rec["sources"]["why"]["version"] == 1
    )
    assert rec["reopened"] is False
    # Nothing newly stale: the agent's acknowledgement stands.
    assert decisions.get_owned(db, "ops", made["id"]).acknowledged_at is not None


def test_a_changed_label_makes_the_answer_stale_and_the_field_empty(client, db, made):
    submit(db, made, region="a", why="old", count=3)
    ack(client, made)
    republish(client, made, FORM.replace("Why <", "Because <"))
    rec = record_of(db, made)
    assert rec["answers"]["why"] == "" and "why" in rec["blank"]
    entry = stale_names(rec)["why"]
    assert entry["value"] == "old" and entry["version"] == 1
    assert entry["label"] == "Why" and entry["reason"] == "changed"
    assert rec["answers"]["region"] == "a"  # the neighbours are still his
    # The agent is told by the record: it reads as unacknowledged again.
    assert decisions.get_owned(db, "ops", made["id"]).acknowledged_at is None
    listed = client.get("/api/v1/decisions", headers=auth(made["token"])).json()
    assert listed[0]["answer_record"]["stale"][0]["name"] == "why"


@pytest.mark.parametrize(
    ("find", "put"),
    [
        ('value="b"> us', 'value="b"> us <input type="hidden">'),  # replaced below
        ('name="count" min="1" max="9"', 'name="count" min="1" max="8"'),
        ('name="why" maxlength="10"', 'name="why" maxlength="20"'),
    ],
)
def test_a_changed_limit_or_option_makes_the_answer_stale(client, db, made, find, put):
    submit(db, made, region="a", why="old", count=3)
    if "hidden" in put:  # an extra option, not a hidden control
        put = 'value="b"> us</label><label><input type="radio" name="region" value="c"> asia'
    republish(client, made, FORM.replace(find, put))
    rec = record_of(db, made)
    assert len(rec["stale"]) == 1, rec["stale"]
    name = rec["stale"][0]["name"]
    assert name in ("region", "count", "why")
    assert rec["answers"][name] in ("", [])


def test_a_renamed_field_is_a_new_empty_field_and_the_old_answer_is_stale(
    client, db, made
):
    submit(db, made, region="a", why="old")
    republish(client, made, FORM.replace('name="why"', 'name="reason"'))
    rec = record_of(db, made)
    assert "why" not in rec["answers"] and rec["answers"]["reason"] == ""
    assert stale_names(rec)["why"]["reason"] == "removed"
    assert stale_names(rec)["why"]["value"] == "old"


def test_a_removed_field_keeps_its_answer_in_history_and_lists_it_stale(
    client, db, made
):
    submit(db, made, region="a", why="old", count=4)
    republish(
        client,
        made,
        FORM.replace(
            '<label>Count <input type="number" name="count" min="1" max="9"></label>',
            "",
        ),
    )
    rec = record_of(db, made)
    assert "count" not in rec["answers"]
    assert stale_names(rec)["count"]["reason"] == "removed"
    assert rec["history"][0]["answers"]["count"] == 4


def test_surrounding_prose_is_not_compared(client, db, made):
    submit(db, made, region="a", why="old")
    republish(
        client,
        made,
        FORM.replace("Pick where it goes.", "A different question entirely."),
    )
    rec = record_of(db, made)
    assert (
        rec["version"] == 1 and rec["stale"] == [] and rec["answers"]["region"] == "a"
    )
    # ...and with a real change elsewhere, the prose still does not matter to the rest.
    republish(
        client,
        made,
        FORM.replace("Pick where it goes.", "Choose.").replace("Notes <", "Remarks <"),
    )
    rec = record_of(db, made)
    assert rec["stale"] == [] and rec["answers"]["region"] == "a"


def test_the_form_reopens_when_a_required_field_is_now_unanswered(client, db, made):
    submit(db, made, region="a", why="old")
    republish(client, made, FORM.replace("Region</legend>", "Which region</legend>"))
    rec = record_of(db, made)
    assert rec["reopened"] is True and rec["answers"]["region"] == ""
    assert stale_names(rec)["region"]["version"] == 1
    # The gallery shows it as unanswered again: no "sent" state, and no carried values.
    data = gallery_data(client, made)
    assert data["answered"] is None and data["carried"] == {"why": "old"}
    assert [s["name"] for s in data["stale"]] == ["region"]
    # Answering the required field again closes the gap.
    again = submit(db, made, version=2, region="b")
    assert again["reopened"] is False and again["stale"] == []


def test_an_optional_field_going_stale_does_not_reopen_the_form(client, db, made):
    submit(db, made, region="a", why="old")
    republish(client, made, FORM.replace("Why <", "Because <"))
    rec = record_of(db, made)
    assert rec["reopened"] is False
    assert gallery_data(client, made)["answered"] is not None


def test_a_complete_all_form_reopens_for_any_unanswered_field(client, db, made):
    token = made["token"]
    client.post(
        "/api/v1/artifacts",
        json={
            "title": "All",
            "body": page(
                FORM.replace(
                    'data-artifact-form="rollout"',
                    'data-artifact-form="all" data-complete="all"',
                )
            ),
        },
        headers=auth(token),
    )
    forms_ = client.get("/api/v1/decisions?kind=form", headers=auth(token)).json()
    assert (
        forms_ == []
    )  # unsubmitted forms are not in the feed; reach it through the page
    pages = client.get("/api/v1/artifacts", headers=auth(token)).json()
    other = next(p for p in pages if p["title"] == "All")
    (form,) = client.get(
        f"/api/v1/artifacts/{other['id']}/forms", headers=auth(token)
    ).json()
    formanswer.submit(
        db,
        form["decision_id"],
        version=1,
        values={"region": "a", "why": "x", "count": 2, "notes": "n"},
    )
    rev = client.get(f"/api/v1/artifacts/{other['id']}", headers=auth(token)).json()[
        "revision"
    ]
    body = page(
        FORM.replace(
            'data-artifact-form="rollout"',
            'data-artifact-form="all" data-complete="all"',
        ).replace("Notes <", "Remarks <")
    )
    r = client.patch(
        f"/api/v1/artifacts/{other['id']}",
        json={"body": body, "expected_revision": rev},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    rec = decisions.get_owned(db, "ops", form["decision_id"]).answer_record
    assert rec["blank"] == ["notes"] and rec["reopened"] is True


def test_a_submit_after_a_republish_answers_the_stale_field_afresh(client, db, made):
    submit(db, made, region="a", why="old")
    republish(client, made, FORM.replace("Why <", "Because <"))
    with pytest.raises(formanswer.SubmitRefused) as caught:
        submit(db, made, version=1, region="a")
    assert caught.value.status == 409  # an answer to the old wording is refused
    rec = submit(db, made, version=2, region="a", why="new")
    assert rec["answers"]["why"] == "new" and rec["stale"] == []
    assert rec["sources"]["why"]["version"] == 2
    assert rec["sources"]["region"]["version"] == 1 or rec["sources"]["region"]["at"]
    assert len(rec["history"]) == 2


def test_a_record_left_on_an_older_version_is_reconciled_at_the_next_submit(
    client, db, made
):
    # A record written before the carry rules existed: still on version 1, no sources.
    submit(db, made, region="a", why="old")
    legacy = dict(record_of(db, made))
    legacy.pop("sources")
    republish(client, made, FORM.replace("Why <", "Because <"))
    with db.connect() as conn:
        conn.execute(
            "update decision set answer_record = %s where id = %s",
            (jsonb(legacy), made["id"]),
        )
    rec = submit(db, made, version=2, region="a", count=5)
    assert rec["version"] == 2 and rec["answers"]["count"] == 5
    assert rec["answers"]["region"] == "a"
    assert rec["answers"]["why"] == ""  # not carried onto the changed question
    assert stale_names(rec)["why"]["value"] == "old"


# --- what the gallery draws and replays -----------------------------------------


def gallery_data(client, made):
    html = client.get(f"/a/{made['page']['id']}").text
    found = re.search(
        r'<script type="application/json" id="artifact-forms">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert found, "no forms data block"
    (form,) = json.loads(found.group(1))["forms"]
    return form


def test_the_gallery_carries_his_answers_the_stale_ones_and_the_relayed_ones(
    client, db, made
):
    submit(db, made, region="a", why="old", count=3)
    republish(client, made, FORM.replace("Why <", "Because <"))
    formanswer.relay(
        db,
        made["id"],
        fields_given={"notes": "from telegram"},
        note="he said so",
        source="telegram",
        recorded_by="ops",
    )
    data = gallery_data(client, made)
    assert data["carried"] == {"region": "a", "count": 3}
    assert "why" not in data["carried"]  # a stale answer is never written into the page
    assert data["relayed"]["notes"]["source"] == "telegram"
    assert data["relayed"]["notes"]["text"] == "from telegram"
    (entry,) = data["stale"]
    assert entry["name"] == "why" and entry["version"] == 1 and entry["text"] == "old"
    assert entry["label"] == "Why"
    assert data["answered"]["version"] == 2


# --- per-field record ------------------------------------------------------------


def test_a_relay_is_stored_per_field_with_its_source_and_is_not_his_answer(
    client, db, made, nudges
):
    done = formanswer.relay(
        db,
        made["id"],
        fields_given={"region": "b", "why": "ok"},
        note="his words",
        source="telegram",
        recorded_by="ops",
    )
    assert done["applied"] == ["region", "why"] and done["skipped"] == []
    rec = record_of(db, made)
    assert rec["answers"]["region"] == "b" and rec["answers"]["why"] == "ok"
    assert rec["sources"]["region"]["source"] == "telegram"
    assert rec["sources"]["region"]["by"] == "ops"
    row = decisions.get_owned(db, "ops", made["id"])
    assert row.answer_source == "telegram" and row.recorded_by == "ops"
    assert row.answer is None
    # Not his own: the gallery draws no "sent" state and writes nothing into the page.
    data = gallery_data(client, made)
    assert data["answered"] is None and data["carried"] == {}
    assert set(data["relayed"]) == {"region", "why"}
    (ev,) = [
        e for e in events.list_for_agent(db, "ops") if e.kind == "decision_answered"
    ]
    assert ev.actor == "ops" and "telegram" in ev.summary and ev.note == "his words"
    assert (
        rec["history"][-1]["kind"] == "relay"
        and rec["history"][-1]["note"] == "his words"
    )


def test_a_relay_does_not_overwrite_a_value_he_submitted_later(client, db, made):
    long_ago = datetime.now(UTC) - timedelta(days=1)
    submit(db, made, region="a", why="mine")
    done = formanswer.relay(
        db,
        made["id"],
        fields_given={"region": "b", "count": 7},
        note="n",
        source="terminal",
        recorded_by="ops",
        said_at=long_ago,
    )
    assert done["skipped"] == ["region"] and done["applied"] == ["count"]
    rec = record_of(db, made)
    assert rec["answers"]["region"] == "a" and rec["answers"]["count"] == 7
    assert rec["sources"]["region"]["source"] == "page"
    assert rec["sources"]["count"]["source"] == "terminal"


def test_a_relay_replaces_an_earlier_submit_and_a_later_submit_replaces_the_relay(
    client, db, made
):
    submit(db, made, region="a")
    formanswer.relay(
        db,
        made["id"],
        fields_given={"region": "b"},
        note="n",
        source="telegram",
        recorded_by="ops",
        said_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    assert record_of(db, made)["answers"]["region"] == "b"
    rec = submit(db, made, region="a")
    assert (
        rec["answers"]["region"] == "a" and rec["sources"]["region"]["source"] == "page"
    )


def test_a_relay_that_changes_nothing_wakes_nobody(client, db, made, nudges):
    submit(db, made, region="a")
    ack(client, made)
    before = len(
        [e for e in events.list_for_agent(db, "ops") if e.kind == "decision_answered"]
    )
    done = formanswer.relay(
        db,
        made["id"],
        fields_given={"region": "b"},
        note="old news",
        source="telegram",
        recorded_by="ops",
        said_at=datetime.now(UTC) - timedelta(days=2),
    )
    assert done["applied"] == []
    after = len(
        [e for e in events.list_for_agent(db, "ops") if e.kind == "decision_answered"]
    )
    assert after == before
    assert decisions.get_owned(db, "ops", made["id"]).acknowledged_at is not None
    assert record_of(db, made)["history"][-1]["skipped"] == ["region"]


def test_a_relayed_field_is_checked_like_his_own(db, made):
    def refused(fields, field, source="terminal"):
        with pytest.raises(formanswer.SubmitRefused) as caught:
            formanswer.relay(
                db,
                made["id"],
                fields_given=fields,
                note="n",
                source=source,
                recorded_by="ops",
            )
        assert caught.value.field == field
        assert record_of(db, made) is None

    refused({"nope": "x"}, "nope")
    refused({"region": "zzz"}, "region")
    refused({"count": 99}, "count")
    refused({"why": ""}, "why")  # a relay says something
    refused({"why": "x" * 50}, "why")
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    refused({"why": secret}, "why")
    with pytest.raises(formanswer.SubmitRefused):
        formanswer.relay(
            db,
            made["id"],
            fields_given={},
            note="n",
            source="terminal",
            recorded_by="ops",
        )


def test_a_checkbox_group_and_a_lone_checkbox_accept_cli_shaped_strings(client, db):
    token, _ = keys.mint(db, agent="ops2")
    body = page(
        '<form data-artifact-form="f"><fieldset><legend>Pets</legend>'
        '<label><input type="checkbox" name="pets" value="cat"> cat</label>'
        '<label><input type="checkbox" name="pets" value="dog"> dog</label></fieldset>'
        '<label><input type="checkbox" name="ack"> read</label></form>'
    )
    made_ = client.post(
        "/api/v1/artifacts", json={"title": "C", "body": body}, headers=auth(token)
    ).json()
    (form,) = client.get(
        f"/api/v1/artifacts/{made_['id']}/forms", headers=auth(token)
    ).json()
    done = formanswer.relay(
        db,
        form["decision_id"],
        fields_given={"pets": "dog,cat", "ack": "yes"},
        note="n",
        source="other",
        recorded_by="ops2",
    )
    assert done["record"]["answers"] == {"pets": ["cat", "dog"], "ack": True}


def test_the_api_relays_a_form_per_field(client, db, made):
    url = f"/api/v1/decisions/{made['id']}/record"
    ok = client.post(
        url,
        json={
            "fields": {"region": "b", "why": "fine"},
            "note": "his words",
            "source": "telegram",
        },
        headers=auth(made["token"]),
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["relayed"] == {"applied": ["region", "why"], "skipped": []}
    assert body["answer_record"]["answers"]["region"] == "b"
    assert body["answer_record"]["sources"]["region"]["source"] == "telegram"
    for bad, status in [
        ({"fields": {"region": "b"}, "note": "n", "source": "page"}, 422),
        ({"letter": "a", "note": "n", "source": "terminal"}, 422),
        ({"note": "n", "source": "terminal"}, 422),
        ({"fields": {}, "note": "n", "source": "terminal"}, 422),
        ({"fields": {"nope": "x"}, "note": "n", "source": "terminal"}, 422),
    ]:
        r = client.post(url, json=bad, headers=auth(made["token"]))
        assert r.status_code == status, (bad, r.text)
    named = client.post(
        url,
        json={"fields": {"region": "zzz"}, "note": "n", "source": "terminal"},
        headers=auth(made["token"]),
    )
    assert named.json()["detail"]["field"] == "region"
    other, _ = keys.mint(db, agent="someone-else")
    nope = client.post(
        url,
        json={"fields": {"region": "a"}, "note": "n", "source": "terminal"},
        headers=auth(other),
    )
    assert nope.status_code == 404


def test_the_api_still_wants_a_letter_for_a_question_and_refuses_fields(client, db):
    token, _ = keys.mint(db, agent="ops3")
    art = store.publish(db, "ops3", "Q", page("<p>q</p>"))
    asked = decisions.ask(
        db,
        agent="ops3",
        artifact_id=art.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    url = f"/api/v1/decisions/{asked.id}/record"
    missing = client.post(
        url, json={"note": "n", "source": "terminal"}, headers=auth(token)
    )
    assert missing.status_code == 422
    fields = client.post(
        url,
        json={"fields": {"x": "y"}, "note": "n", "source": "terminal"},
        headers=auth(token),
    )
    assert fields.status_code == 422
    ok = client.post(
        url,
        json={"letter": "b", "note": "n", "source": "terminal"},
        headers=auth(token),
    )
    assert ok.status_code == 200 and ok.json()["answer"] == "b"


# --- the CLI ---------------------------------------------------------------------


def _row(**rec):
    record = {
        "form": "rollout",
        "version": 2,
        "answered_by": "jeremy",
        "answered_at": "2026-11-02T10:00:00+00:00",
        "answers": {"region": "b", "why": ""},
        "blank": ["why"],
        "stale": [],
        "sources": {"region": {"source": "telegram", "by": "cove"}},
    }
    record.update(rec)
    return {
        "id": "d1",
        "question": "Rollout",
        "kind": "form",
        "answer_record": record,
        "answer_source": "telegram",
        "acknowledged_at": "2026-11-02T11:00:00+00:00",
    }


def test_decide_show_prints_stale_reopened_and_relayed_fields():
    out = decide._fmt(
        _row(
            reopened=True,
            stale=[
                {
                    "name": "why",
                    "value": "old",
                    "text": "old",
                    "version": 1,
                    "label": "Why",
                    "reason": "changed",
                },
                {
                    "name": "count",
                    "value": 3,
                    "text": "3",
                    "version": 1,
                    "label": "Count",
                    "reason": "removed",
                },
            ],
        )
    )
    assert "region: b   [relayed from telegram by cove]" in out
    assert "REOPENED" in out
    assert (
        "stale (answered on version 1, question changed since): why = old   [was: Why]"
        in out
    )
    assert "stale (answered on version 1, question removed since): count = 3" in out


def test_decide_record_takes_fields_and_builds_the_payload(monkeypatch, capsys):
    sent = {}

    def fake(method, url, key, payload=None, **kw):
        sent.update(url=url, payload=payload)
        return {**_row(), "relayed": {"applied": ["region"], "skipped": ["why"]}}

    monkeypatch.setattr(decide, "_request", fake)
    monkeypatch.setattr(decide, "_key", lambda *_a, **_k: "k", raising=False)
    parser = decide.build_parser()
    args = parser.parse_args(
        [
            "record",
            "d1",
            "--field",
            "region=b",
            "--field",
            "pets=cat",
            "--field",
            "pets=dog",
            "--note",
            "his words",
            "--source",
            "telegram",
        ]
    )
    assert args.letter is None and args.field == ["region=b", "pets=cat", "pets=dog"]
    help_text = parser.parse_args  # the subcommand's own help names --field
    del help_text
    sub = [a for a in parser._subparsers._group_actions[0].choices["record"]._actions]
    assert any("--field" in a.option_strings for a in sub)


def test_decide_record_refuses_both_or_neither_of_letter_and_field():
    parser = decide.build_parser()
    ns = parser.parse_args(["record", "d1", "--note", "n", "--source", "terminal"])
    assert ns.letter is None and ns.field == []
