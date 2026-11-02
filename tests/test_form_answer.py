"""Form controls inside a page, slice 2: validating a submitted batch against the stored
field list, recording it on the form's decision, the event and the nudge, the gallery's
submit route, and what the agent reads back (docs/agent/specs/2026-10-29-form-controls-design.md
3.3-3.4)."""

import pytest
from fastapi.testclient import TestClient

from artifacts import decide, decisions, events, formanswer, keys, nudge, store
from artifacts.app import create_gallery_app

ORIGIN = "https://artifacts.tenfourty.site"
SAME = {"sec-fetch-site": "same-origin"}

FORM = """
<form data-artifact-form="rollout">
  <fieldset name="region"><legend>Region</legend>
    <label><input type="radio" name="region" value="a" required> eu</label>
    <label><input type="radio" name="region" value="b"> us</label>
  </fieldset>
  <label>Count <input type="number" name="count" min="1" max="9" step="2"></label>
  <fieldset><legend>Pets</legend>
    <label><input type="checkbox" name="pets" value="cat"> cat</label>
    <label><input type="checkbox" name="pets" value="dog"> dog</label>
  </fieldset>
  <label><input type="checkbox" name="ack"> read it</label>
  <label>Size <select name="size"><option value="">pick</option>
    <option value="s">S</option><option value="l">L</option></select></label>
  <label>Day <input type="date" name="day"></label>
  <label>When <input type="datetime-local" name="when"></label>
  <label>Why <input type="text" name="why" maxlength="10"></label>
  <label>Notes <textarea name="notes"></textarea></label>
</form>
"""


def page(inner: str) -> str:
    return (
        "<!doctype html><html><head><title>P</title></head><body>"
        + inner
        + "</body></html>"
    )


def spec_of(inner=FORM):
    from artifacts import forms

    (form,) = forms.parse(page(inner)).forms
    return form.fields, form.complete_all


def check(values, inner=FORM):
    fields, complete = spec_of(inner)
    return formanswer.validate(fields, complete, values)


def refused(values, field, inner=FORM, status=422):
    with pytest.raises(formanswer.SubmitRefused) as caught:
        check(values, inner)
    assert caught.value.field == field, caught.value.detail
    assert caught.value.status == status
    return caught.value


# --- validate, per field type ------------------------------------------------


def test_a_full_batch_is_kept_in_the_shape_the_agent_reads():
    answers, blank = check(
        {
            "region": "a",
            "count": "3",
            "pets": ["dog", "cat"],
            "ack": True,
            "size": "l",
            "day": "2026-11-02",
            "when": "2026-11-02T06:00",
            "why": "ok",
            "notes": "x\ny",
        }
    )
    assert answers == {
        "region": "a",
        "count": 3,
        "pets": ["cat", "dog"],  # the page's order, not the order sent
        "ack": True,
        "size": "l",
        "day": "2026-11-02",
        "when": "2026-11-02T06:00",
        "why": "ok",
        "notes": "x\ny",
    }
    assert blank == []


def test_blanks_are_named_and_a_lone_checkbox_is_never_blank():
    answers, blank = check({"region": "b"})
    assert answers["ack"] is False and "ack" not in blank
    assert answers["pets"] == [] and answers["size"] == ""
    assert set(blank) == {"count", "pets", "size", "day", "when", "why", "notes"}
    answers, blank = check({"region": "a", "why": "   ", "size": "", "pets": []})
    assert {"why", "size", "pets"} <= set(blank)


def test_a_radio_or_select_value_outside_its_options_is_refused():
    refused({"region": "z"}, "region")
    refused({"region": "a", "size": "xl"}, "size")
    refused({"region": ["a"]}, "region")


def test_a_checkbox_group_takes_a_list_of_known_values_once_each():
    refused({"region": "a", "pets": "cat"}, "pets")
    refused({"region": "a", "pets": ["cat", "cat"]}, "pets")
    refused({"region": "a", "pets": ["fish"]}, "pets")
    refused({"region": "a", "ack": "yes"}, "ack")


def test_number_limits_and_step():
    refused({"region": "a", "count": "0"}, "count")
    refused({"region": "a", "count": 11}, "count")
    refused({"region": "a", "count": 2}, "count")  # step 2 from min 1
    refused({"region": "a", "count": "abc"}, "count")
    refused({"region": "a", "count": "nan"}, "count")
    refused({"region": "a", "count": True}, "count")
    refused({"region": "a", "count": 10**400}, "count")  # float() overflows: a 422
    assert check({"region": "a", "count": 5})[0]["count"] == 5


def test_dates_must_be_real_and_in_the_native_shape():
    refused({"region": "a", "day": "2026-02-30"}, "day")
    refused({"region": "a", "day": "02/11/2026"}, "day")
    refused({"region": "a", "when": "2026-11-02"}, "when")
    refused({"region": "a", "when": "2026-13-02T06:00"}, "when")


def test_text_limits_and_control_characters():
    refused({"region": "a", "why": "x" * 11}, "why")
    refused({"region": "a", "why": "a\nb"}, "why")  # single line
    refused({"region": "a", "notes": "a\x00b"}, "notes")
    assert check({"region": "a", "notes": "a\nb"})[0]["notes"] == "a\nb"


def test_a_name_the_form_does_not_have_is_refused():
    refused({"region": "a", "sneaky": "1"}, "sneaky")
    with pytest.raises(formanswer.SubmitRefused):
        check([])


def test_required_and_complete_all_name_the_blank_field():
    assert refused({}, "region").detail == "this field is required"
    inner = '<form data-artifact-form="f" data-complete="all"><input name="a"><input name="b"></form>'
    err = refused({"a": "x"}, "b", inner)
    assert "every field" in err.detail
    assert check({"a": "x", "b": "y"}, inner)[1] == []


# --- the service: submit, event, nudge, merge ---------------------------------


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", ORIGIN)
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


@pytest.fixture()
def nudges(monkeypatch):
    sent = []
    monkeypatch.setattr(nudge, "emit", lambda kind, **kw: sent.append((kind, kw)))
    return sent


def auth(token):
    return {"Authorization": f"Bearer {token}"}


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
    return {"token": token, "page": page_, "form": form, "id": form["decision_id"]}


def record_of(db, decision_id):
    return decisions.get_owned(db, "ops", decision_id).answer_record


def test_a_submit_records_the_answer_event_and_nudge(client, db, made, nudges):
    rec = formanswer.submit(
        db, made["id"], version=1, values={"region": "a", "count": 3, "why": "go"}
    )
    assert rec["kind"] == "form" and rec["form"] == "rollout" and rec["version"] == 1
    assert rec["answered_by"] == "jeremy" and rec["stale"] == []
    assert rec["answers"]["region"] == "a" and rec["answers"]["count"] == 3
    assert "notes" in rec["blank"] and "region" not in rec["blank"]
    assert rec["sent"] == ["region", "count", "ack", "why"]
    row = decisions.get_owned(db, "ops", made["id"])
    assert row.answer is None and row.answered_at is not None
    assert row.answer_source == "page" and row.recorded_by is None
    found = events.list_for_agent(db, "ops")
    (ev,) = [e for e in found if e.kind == "decision_answered"]
    assert ev.ref_id == made["id"] and ev.actor == "jeremy"
    assert ev.summary.startswith("form rollout: ")
    assert [k for k, _ in nudges] == ["decision_answered"]


def test_a_submit_resets_the_acknowledgement_and_a_second_is_a_new_event(
    client, db, made, nudges
):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"})
    ack = client.post(
        f"/api/v1/decisions/{made['id']}/acknowledge", headers=auth(made["token"])
    )
    assert ack.json()["acknowledged_at"]
    formanswer.submit(db, made["id"], version=1, values={"region": "b"})
    assert decisions.get_owned(db, "ops", made["id"]).acknowledged_at is None
    kinds = [e.kind for e in events.list_for_agent(db, "ops")]
    assert kinds.count("decision_answered") == 2 and len(nudges) == 2
    rec = record_of(db, made["id"])
    assert rec["answers"]["region"] == "b"  # the later value wins
    assert len(rec["history"]) == 2


def test_a_blank_in_a_second_sitting_never_erases_an_earlier_answer(db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "a", "why": "first"})
    rec = formanswer.submit(
        db, made["id"], version=1, values={"region": "b", "count": 5}
    )
    assert rec["answers"]["why"] == "first" and rec["answers"]["count"] == 5
    assert rec["answers"]["region"] == "b"
    assert "why" not in rec["blank"]
    assert rec["sent"] == ["region", "count", "ack"]  # what THIS submit carried
    assert rec["history"][0]["answers"]["region"] == "a"


def test_a_republish_starts_a_fresh_record_and_an_old_version_is_a_409(
    client, db, made
):
    formanswer.submit(db, made["id"], version=1, values={"region": "a", "why": "old"})
    again = client.patch(
        f"/api/v1/artifacts/{made['page']['id']}",
        json={
            "body": page(FORM.replace("Notes", "Remarks")),
            "expected_revision": made["page"]["revision"],
        },
        headers=auth(made["token"]),
    )
    assert again.status_code == 200, again.text
    with pytest.raises(formanswer.SubmitRefused) as caught:
        formanswer.submit(db, made["id"], version=1, values={"region": "a"})
    assert caught.value.status == 409
    rec = formanswer.submit(db, made["id"], version=2, values={"region": "b"})
    assert rec["version"] == 2 and rec["answers"]["why"] == ""  # not carried
    assert rec["answers"]["region"] == "b"
    assert len(rec["history"]) == 2 and rec["history"][0]["version"] == 1


def test_a_closed_or_unknown_form_is_not_found(client, db, made):
    client.post(f"/api/v1/decisions/{made['id']}/close", headers=auth(made["token"]))
    with pytest.raises(store.NotFound):
        formanswer.submit(db, made["id"], version=1, values={"region": "a"})
    with pytest.raises(store.NotFound):
        formanswer.submit(db, "nope", version=1, values={"region": "a"})


def test_a_secret_in_a_text_field_is_refused_naming_it_and_stores_nothing(
    db, made, nudges
):
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    with pytest.raises(formanswer.SubmitRefused) as caught:
        formanswer.submit(
            db, made["id"], version=1, values={"region": "a", "notes": f"key {secret}"}
        )
    assert caught.value.field == "notes" and secret not in caught.value.detail
    assert record_of(db, made["id"]) is None and nudges == []


# --- the route -----------------------------------------------------------------


def post(client, made, body, headers=SAME, **kw):
    return client.post(
        f"/a/{made['page']['id']}/forms/rollout", json=body, headers=headers, **kw
    )


def test_the_route_is_same_origin_guarded_and_wants_json(client, db, made, nudges):
    body = {"version": 1, "values": {"region": "a"}}
    assert post(client, made, body, headers={}).status_code == 403
    assert (
        post(client, made, body, headers={"sec-fetch-site": "cross-site"}).status_code
        == 403
    )
    assert record_of(db, made["id"]) is None
    form_encoded = client.post(
        f"/a/{made['page']['id']}/forms/rollout", data={"version": "1"}, headers=SAME
    )
    assert form_encoded.status_code == 415
    ok = post(client, made, body)
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True and ok.json()["answered"] >= 1
    assert record_of(db, made["id"])["answers"]["region"] == "a"


def test_the_route_answers_422_naming_the_field_and_409_for_a_stale_version(
    client, made
):
    bad = post(client, made, {"version": 1, "values": {"region": "zzz"}})
    assert bad.status_code == 422
    assert bad.json()["field"] == "region" and bad.json()["detail"]
    missing = post(client, made, {"version": 1, "values": {}})
    assert missing.status_code == 422 and missing.json()["field"] == "region"
    stale = post(client, made, {"version": 9, "values": {"region": "a"}})
    assert stale.status_code == 409
    no_version = post(client, made, {"values": {"region": "a"}})
    assert no_version.status_code == 422
    assert post(client, made, [1]).status_code == 422
    unknown = client.post(
        f"/a/{made['page']['id']}/forms/nope",
        json={"version": 1, "values": {}},
        headers=SAME,
    )
    assert unknown.status_code == 404


def test_the_route_refuses_a_secret_without_echoing_it(client, db, made):
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = post(
        client, made, {"version": 1, "values": {"region": "a", "why": secret}}
    )
    assert response.status_code == 422 and response.json()["field"] == "why"
    assert secret not in response.text and record_of(db, made["id"]) is None


def test_the_gallery_page_carries_the_stored_fields_and_the_script(client, made):
    html = client.get(f"/a/{made['page']['id']}").text
    assert 'id="artifact-forms"' in html and '<script src="/forms.js" defer>' in html
    assert '"form":"rollout"' in html.replace(" ", "")
    js = client.get("/forms.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert js.headers["cache-control"] == "no-cache"
    assert "content-security-policy" in js.headers
    assert "textContent" in js.text and "innerHTML" not in js.text
    assert "e.source!==frame.contentWindow||e.origin!==origin" in js.text


# --- what the agent reads back --------------------------------------------------


def test_an_answered_form_reaches_the_agents_list_and_an_unsubmitted_one_does_not(
    client, db, made
):
    assert client.get("/api/v1/decisions", headers=auth(made["token"])).json() == []
    formanswer.submit(db, made["id"], version=1, values={"region": "a"})
    (row,) = client.get("/api/v1/decisions", headers=auth(made["token"])).json()
    assert row["kind"] == "form" and row["answer"] is None
    assert row["answer_record"]["answers"]["region"] == "a"
    mine = decisions.list_for_agent(db, "ops", include_answered=True)
    assert [r.id for r in mine] == [made["id"]]
    only_open = client.get(
        "/api/v1/decisions", params={"open_only": "true"}, headers=auth(made["token"])
    ).json()
    assert only_open == []
    # the single read and acknowledge work on it too
    ack = client.post(
        f"/api/v1/decisions/{made['id']}/acknowledge", headers=auth(made["token"])
    )
    assert ack.status_code == 200


def test_wait_hears_a_submitted_form(client, db, made):
    before = events.latest_seq(db, "ops")
    formanswer.submit(db, made["id"], version=1, values={"region": "a"})
    woke = events.list_for_agent(db, "ops", since=before)
    assert [e.kind for e in woke] == ["decision_answered"]


# --- the CLI's reading of it ------------------------------------------------------


def _row(**over):
    row = {
        "id": "d1",
        "question": "Rollout",
        "kind": "form",
        "artifact_title": "Board",
        "agent": "ops",
        "answer": None,
        "answer_source": "page",
        "acknowledged_at": None,
        "answer_record": {
            "form": "rollout",
            "version": 3,
            "answered_by": "jeremy",
            "answered_at": "2026-11-02T10:00:00+00:00",
            "answers": {"region": "a", "pets": ["cat", "dog"], "ack": True, "why": ""},
            "blank": ["why"],
            "stale": [],
        },
    }
    row.update(over)
    return row


def test_decide_prints_a_form_record_field_by_field():
    out = decide._fmt(_row())
    assert "FORM rollout (version 3, he submitted it, by jeremy" in out
    assert "  region: a" in out and "  pets: cat, dog" in out
    assert "  ack: yes" in out and "  why: (blank)" in out
    assert "blank: why" in out
    assert "artifacts-decide ack d1" in out


def test_decide_counts_a_submitted_form_as_a_new_answer_until_acknowledged():
    fresh, done = _row(), _row(acknowledged_at="2026-11-02T11:00:00+00:00")
    pending = _row(answer_record=None)
    assert decide._new_answers([fresh, done, pending]) == [fresh]
    assert "NOT YET ACKNOWLEDGED" not in decide._fmt(done)
    assert "stale" not in decide._fmt(_row())
    stale = _row()
    stale["answer_record"] = {**stale["answer_record"], "stale": ["count"]}
    assert "stale" in decide._fmt(stale)


def _aside_events(db, decision_id):
    return [
        e.kind
        for e in events.list_for_agent(db, "ops")
        if e.kind in ("decision_set_aside", "decision_reopened")
        and e.ref_id == decision_id
    ]


def test_a_submit_on_an_archived_pages_form_is_refused(db, made):
    assert store.set_archived(db, made["page"]["id"], True, agent="ops")
    with pytest.raises(store.NotFound):
        formanswer.submit(db, made["id"], version=1, values={"region": "a"})
    assert record_of(db, made["id"]) is None
    # Back on the wall, it takes a submit again.
    assert store.set_archived(db, made["page"]["id"], False, agent="ops")
    formanswer.submit(db, made["id"], version=1, values={"region": "a"})
    assert record_of(db, made["id"]) is not None


def test_archiving_a_page_with_a_submitted_form_does_not_set_it_aside(db, made):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"})
    assert store.set_archived(db, made["page"]["id"], True, agent="ops")
    row = decisions.get_owned(db, "ops", made["id"])
    assert not row.set_aside
    assert store.set_archived(db, made["page"]["id"], False, agent="ops")
    assert decisions.get_owned(db, "ops", made["id"]).reopened_at is None
    assert _aside_events(db, made["id"]) == []


def test_an_unsubmitted_form_is_set_aside_and_reopened(db, made):
    assert store.set_archived(db, made["page"]["id"], True, agent="ops")
    assert decisions.get_owned(db, "ops", made["id"]).set_aside
    assert store.set_archived(db, made["page"]["id"], False, agent="ops")
    back = decisions.get_owned(db, "ops", made["id"])
    assert not back.set_aside and back.reopened_at is not None
    assert _aside_events(db, made["id"]) == ["decision_set_aside", "decision_reopened"]


def _patch(client, made, body):
    return client.patch(
        f"/api/v1/artifacts/{made['page']['id']}",
        json={"body": body, "expected_revision": made["page"]["revision"]},
        headers=auth(made["token"]),
    )


def test_a_submitted_form_survives_a_republish_without_it_until_acknowledged(
    client, db, made
):
    formanswer.submit(db, made["id"], version=1, values={"region": "a"})
    assert _patch(client, made, page("<p>no form now</p>")).status_code == 200
    listed = client.get("/api/v1/decisions", headers=auth(made["token"])).json()
    assert [r["id"] for r in listed] == [made["id"]]
    assert listed[0]["answer_record"]["answers"]["region"] == "a"
    assert [r.id for r in decisions.list_for_agent(db, "ops")] == [made["id"]]
    ack = client.post(
        f"/api/v1/decisions/{made['id']}/acknowledge", headers=auth(made["token"])
    )
    assert ack.status_code == 200
    assert client.get("/api/v1/decisions", headers=auth(made["token"])).json() == []
    assert decisions.list_for_agent(db, "ops") == []


def test_an_unrelated_republish_keeps_the_form_version_and_a_changed_field_does_not(
    client, db, made
):
    assert (
        _patch(client, made, page(FORM + "<p>an unrelated edit</p>")).status_code == 200
    )
    (form,) = client.get(
        f"/api/v1/artifacts/{made['page']['id']}/forms", headers=auth(made["token"])
    ).json()
    assert form["version"] == 1
    rec = formanswer.submit(db, made["id"], version=1, values={"region": "a"})
    assert rec["version"] == 1
    rev = client.get(
        f"/api/v1/artifacts/{made['page']['id']}", headers=auth(made["token"])
    ).json()["revision"]
    changed = client.patch(
        f"/api/v1/artifacts/{made['page']['id']}",
        json={
            "body": page(FORM.replace("Notes", "Remarks")),
            "expected_revision": rev,
        },
        headers=auth(made["token"]),
    )
    assert changed.status_code == 200, changed.text
    with pytest.raises(formanswer.SubmitRefused) as caught:
        formanswer.submit(db, made["id"], version=1, values={"region": "b"})
    assert caught.value.status == 409


def test_the_route_refuses_a_body_over_a_megabyte(client, db, made):
    big = {"version": 1, "values": {"region": "a", "notes": "x" * 1_100_000}}
    assert post(client, made, big).status_code == 413
    assert record_of(db, made["id"]) is None
    raw = client.post(
        f"/a/{made['page']['id']}/forms/rollout",
        content=b"{" + b" " * 1_100_000 + b"}",
        headers={**SAME, "content-type": "application/json"},
    )
    assert raw.status_code == 413


def test_an_archived_pages_gallery_view_draws_no_submit_bar(client, db, made):
    assert 'id="artifact-forms"' in client.get(f"/a/{made['page']['id']}").text
    assert store.set_archived(db, made["page"]["id"], True, agent="ops")
    html = client.get(f"/a/{made['page']['id']}").text
    assert 'id="artifact-forms"' not in html and "/forms.js" not in html
