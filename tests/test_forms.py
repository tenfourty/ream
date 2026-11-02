"""Form controls inside a page, slice 1: the parser and validator, the stored field
lists, the `form` decision, the read-back and the places a form must NOT yet show
(docs/agent/specs/2026-10-29-form-controls-design.md)."""

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, forms, keys
from artifacts.app import create_gallery_app

HEAD = "<!doctype html><html><head><title>P</title></head><body>"
TAIL = "</body></html>"

ROLLOUT = """
<form data-artifact-form="rollout">
  <fieldset name="region"><legend>Region</legend>
    <label><input type="radio" name="region" value="a" required> a. eu (recommended)</label>
    <label><input type="radio" name="region" value="b"> b. us</label>
  </fieldset>
  <table>
    <tr><td>caps-unit-trim</td><td>
      <label><input type="radio" name="wt.caps-unit-trim" value="keep"> keep</label>
      <label><input type="radio" name="wt.caps-unit-trim" value="park"> park</label></td></tr>
  </table>
  <label>Window <input type="datetime-local" name="window"></label>
  <label>Count <input type="number" name="count" min="1" max="9" step="2" required></label>
  <label>Day <input type="date" name="day"></label>
  <label>Size <select name="size"><option value="">pick</option>
    <option value="s">Small</option><option value="l">Large</option></select></label>
  <label><input type="checkbox" name="ack"> I have read it</label>
  <fieldset><legend>Pets</legend>
    <label><input type="checkbox" name="pets" value="cat"> cat</label>
    <label><input type="checkbox" name="pets" value="dog"> dog</label>
  </fieldset>
  <label>Why <input type="text" name="why" maxlength="40"></label>
  <label>Anything else <textarea name="notes" maxlength="500">x y</textarea></label>
</form>
"""


def page(inner: str) -> str:
    return HEAD + inner + TAIL


def fields(parsed: forms.Parsed, form: str = "rollout") -> dict[str, dict]:
    (found,) = [f for f in parsed.forms if f.name == form]
    return {f["name"]: f for f in found.fields}


# --- the parser -------------------------------------------------------------


def test_a_page_with_no_controls_has_no_forms():
    parsed = forms.parse(page("<p>hello</p>"))
    assert parsed.forms == [] and parsed.stray_line is None


def test_every_supported_control_is_read_with_label_options_and_limits():
    parsed = forms.parse(page(ROLLOUT))
    (form,) = parsed.forms
    assert form.name == "rollout" and form.title == "rollout"
    assert form.complete_all is False
    f = fields(parsed)
    assert list(f) == [
        "region",
        "wt.caps-unit-trim",
        "window",
        "count",
        "day",
        "size",
        "ack",
        "pets",
        "why",
        "notes",
    ]
    assert f["region"] == {
        "name": "region",
        "type": "radio",
        "required": True,
        "label": "Region",
        "options": [
            {"value": "a", "label": "a. eu (recommended)"},
            {"value": "b", "label": "b. us"},
        ],
    }
    assert f["wt.caps-unit-trim"]["required"] is False
    assert [o["value"] for o in f["wt.caps-unit-trim"]["options"]] == ["keep", "park"]
    assert f["wt.caps-unit-trim"]["label"] == "wt.caps-unit-trim"
    assert f["window"]["type"] == "datetime-local" and f["window"]["label"] == "Window"
    assert (f["count"]["min"], f["count"]["max"], f["count"]["step"]) == (1, 9, 2)
    assert f["count"]["required"] is True
    assert f["day"]["type"] == "date"
    # The placeholder option (value "") is not a choice.
    assert f["size"]["options"] == [
        {"value": "s", "label": "Small"},
        {"value": "l", "label": "Large"},
    ]
    assert f["size"]["label"] == "Size"
    # A lone checkbox is yes or no; a group is a list of the checked values.
    assert f["ack"]["options"] == [] and f["ack"]["label"] == "I have read it"
    assert f["pets"]["label"] == "Pets"
    assert [o["value"] for o in f["pets"]["options"]] == ["cat", "dog"]
    assert f["why"]["maxlength"] == 40 and f["notes"]["maxlength"] == 500
    # A textarea's own text is neither its label nor part of one.
    assert f["notes"]["label"] == "Anything else"


def test_complete_all_and_the_title():
    parsed = forms.parse(
        page(
            '<div data-artifact-form="rel" data-complete="all" aria-label="Release">'
            '<input type="text" name="a" aria-label="A"></div>'
        )
    )
    (form,) = parsed.forms
    assert form.complete_all is True and form.title == "Release"
    assert form.fields[0]["label"] == "A"


def test_the_title_is_the_legend_of_a_fieldset_form():
    parsed = forms.parse(
        page(
            '<fieldset data-artifact-form="f"><legend>Pick one</legend>'
            '<input name="x"></fieldset>'
        )
    )
    assert parsed.forms[0].title == "Pick one"


def test_label_for_resolves_in_either_order():
    parsed = forms.parse(
        page(
            '<form data-artifact-form="f"><label for="a">Before</label>'
            '<input id="a" name="a"><input id="b" name="b">'
            '<label for="b">After</label></form>'
        )
    )
    f = fields(parsed, "f")
    assert (f["a"]["label"], f["b"]["label"]) == ("Before", "After")


def test_two_forms_on_one_page():
    parsed = forms.parse(
        page(
            '<form data-artifact-form="one"><input name="a"></form>'
            '<div data-artifact-form="two"><input name="a"></div>'
        )
    )
    assert [f.name for f in parsed.forms] == ["one", "two"]


def test_unclosed_elements_inside_the_form_do_not_leak_it():
    parsed = forms.parse(
        page(
            '<form data-artifact-form="f"><p>text<table><tr><td>'
            '<input name="a"></table></form><input name="outside">'
        )
    )
    assert [x["name"] for x in parsed.forms[0].fields] == ["a"]
    assert parsed.stray_line is not None


@pytest.mark.parametrize(
    ("inner", "needle", "control"),
    [
        ('<input type="file" name="f">', "type='file'", "f"),
        ('<input type="hidden" name="h" value="1">', "type='hidden'", "h"),
        ('<input type="password" name="p">', "type='password'", "p"),
        ('<input type="range" name="r">', "type='range'", "r"),
        ('<input type="submit" name="s">', "type='submit'", "s"),
        ('<input type="email" name="e">', "type='email'", "e"),
        ("<button name=b>Go</button>", "button is not supported", "b"),
        (
            '<select name="m" multiple><option value="a">a</option></select>',
            "select multiple",
            "m",
        ),
        ('<input name="">', "has no name", None),
        ("<input>", "has no name", None),
        ('<input name="bad name">', "control names use", "bad name"),
        (
            '<input type="radio" name="r" value="a">',
            "at least two options",
            "r",
        ),
        (
            '<input type="radio" name="r" value="a"><input type="radio" name="r">',
            "non-empty value",
            "r",
        ),
        (
            '<input type="radio" name="r" value="a"><input type="radio" name="r" value="a">',
            "share a value",
            "r",
        ),
        ('<input name="d"><input name="d">', "share a name", "d"),
        (
            '<input type="radio" name="d" value="a"><input type="checkbox" name="d">',
            "different types",
            "d",
        ),
        ('<input type="number" name="n" min="5" max="1">', "min is above max", "n"),
        ('<input type="number" name="n" min="abc">', "not a number", "n"),
        ('<input type="number" name="n" step="0">', "step must be above zero", "n"),
        ('<input name="t" maxlength="0">', "maxlength", "t"),
        ('<input name="t" maxlength="x">', "maxlength", "t"),
        (
            '<select name="s"><option value="">none</option></select>',
            "at least one",
            "s",
        ),
        (
            '<select name="s"><option value="a">1</option><option>a</option></select>',
            "share a value",
            "s",
        ),
        ("<p>no controls</p>", "at least one control", None),
    ],
)
def test_a_bad_form_is_refused_naming_the_form_and_control(inner, needle, control):
    with pytest.raises(forms.FormRefused) as caught:
        forms.parse(page(f'<form data-artifact-form="f">{inner}</form>'))
    detail = caught.value.as_detail()
    assert needle in detail["detail"]
    assert detail["form"] == "f"
    assert detail.get("control") == control


def test_form_level_refusals():
    def refused(inner):
        with pytest.raises(forms.FormRefused) as caught:
            forms.parse(page(inner))
        return caught.value

    assert (
        "share a name"
        in refused(
            '<form data-artifact-form="f"><input name="a"></form>'
            '<form data-artifact-form="f"><input name="a"></form>'
        ).detail
    )
    assert (
        "nested"
        in refused(
            '<div data-artifact-form="a"><div data-artifact-form="b">'
            '<input name="x"></div></div>'
        ).detail
    )
    assert (
        "needs a name"
        in refused('<form data-artifact-form=""><input name="x"></form>').detail
    )
    assert (
        "data-complete"
        in refused(
            '<form data-artifact-form="f" data-complete="some"><input name="x"></form>'
        ).detail
    )


def test_the_limits_on_forms_and_fields():
    many_fields = "".join(f'<input name="n{i}">' for i in range(201))
    with pytest.raises(forms.FormRefused, match="more than 200"):
        forms.parse(page(f'<form data-artifact-form="f">{many_fields}</form>'))
    many_forms = "".join(
        f'<form data-artifact-form="f{i}"><input name="a"></form>' for i in range(51)
    )
    with pytest.raises(forms.FormRefused, match="more than 50"):
        forms.parse(page(many_forms))


def test_text_in_a_script_is_not_a_control():
    parsed = forms.parse(page('<script>document.write("<input name=a>")</script>'))
    assert parsed.stray_line is None and parsed.forms == []


# --- publish-time warnings ----------------------------------------------------


def test_a_control_outside_a_form_warns_with_its_line():
    body = HEAD + "\n<p>x</p>\n<input name='q'>\n" + TAIL
    (warning,) = forms.warnings(body)
    assert warning["shape"] == "control_outside_form" and warning["line"] == 3
    assert "data-artifact-form" in warning["hint"]


def test_a_page_with_only_forms_has_no_warning():
    assert forms.warnings(page(ROLLOUT)) == []
    assert forms.warnings(page("<p>nothing</p>")) == []


# --- the service --------------------------------------------------------------


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def publish(client, token, body, title="Board"):
    return client.post(
        "/api/v1/artifacts", json={"title": title, "body": body}, headers=auth(token)
    )


def read_forms(client, token, artifact_id, **params):
    return client.get(
        f"/api/v1/artifacts/{artifact_id}/forms", params=params, headers=auth(token)
    )


def test_publish_stores_each_form_as_one_decision_and_reads_back(client, db):
    token, _ = keys.mint(db, agent="ops")
    response = publish(
        client,
        token,
        page(
            ROLLOUT + '<form data-artifact-form="second" data-complete="all">'
            '<label>Name <input name="n"></label></form>'
        ),
    )
    assert response.status_code == 201, response.text
    assert response.json()["warnings"] == []
    artifact_id = response.json()["id"]
    got = read_forms(client, token, artifact_id).json()
    assert [f["form"] for f in got] == ["rollout", "second"]
    rollout, second = got
    assert rollout["version"] == 1 and rollout["complete"] is None
    assert second["complete"] == "all"
    assert [f["name"] for f in second["fields"]] == ["n"]
    assert rollout["fields"][0]["options"][0] == {
        "value": "a",
        "label": "a. eu (recommended)",
    }
    # Each form is a decision of kind form, with the title as its question.
    listed = client.get(
        f"/api/v1/artifacts/{artifact_id}/decisions", headers=auth(token)
    ).json()
    assert {(d["kind"], d["question"], len(d["options"])) for d in listed} == {
        ("form", "rollout", 0),
        ("form", "second", 0),
    }
    assert {d["form"]["form"] for d in listed} == {"rollout", "second"}
    assert {d["id"] for d in listed} == {f["decision_id"] for f in got}


def test_a_bad_form_is_a_422_and_stores_nothing(client, db):
    token, _ = keys.mint(db, agent="ops")
    bad = page('<form data-artifact-form="f"><input type="file" name="up"></form>')
    response = publish(client, token, bad)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["form"] == "f" and detail["control"] == "up"
    assert "file" in detail["detail"] and detail["line"] == 1
    assert client.get("/api/v1/artifacts", headers=auth(token)).json() == []


def test_a_bad_form_on_update_is_a_422_and_leaves_the_page_alone(client, db):
    token, _ = keys.mint(db, agent="ops")
    made = publish(client, token, page(ROLLOUT)).json()
    response = client.patch(
        f"/api/v1/artifacts/{made['id']}",
        json={
            "body": page('<form data-artifact-form="f"><button>Go</button></form>'),
            "expected_revision": made["revision"],
        },
        headers=auth(token),
    )
    assert response.status_code == 422
    assert "control" not in response.json()["detail"]
    assert [f["form"] for f in read_forms(client, token, made["id"]).json()] == [
        "rollout"
    ]


def test_republish_keeps_the_decision_and_stores_the_new_fields(client, db):
    token, _ = keys.mint(db, agent="ops")
    made = publish(
        client,
        token,
        page('<form data-artifact-form="f"><label>One <input name="a"></label></form>'),
    ).json()
    (before,) = read_forms(client, token, made["id"]).json()
    changed = page(
        '<form data-artifact-form="f"><label>One <input name="a"></label>'
        '<label>Two <input name="b"></label></form>'
    )
    again = client.patch(
        f"/api/v1/artifacts/{made['id']}",
        json={"body": changed, "expected_revision": made["revision"]},
        headers=auth(token),
    )
    assert again.status_code == 200, again.text
    (after,) = read_forms(client, token, made["id"]).json()
    assert after["decision_id"] == before["decision_id"]
    assert after["version"] == 2 and after["first_version"] == 1
    assert [f["name"] for f in after["fields"]] == ["a", "b"]


def test_a_form_removed_from_the_page_is_closed(client, db):
    token, _ = keys.mint(db, agent="ops")
    made = publish(client, token, page(ROLLOUT)).json()
    client.patch(
        f"/api/v1/artifacts/{made['id']}",
        json={"body": page("<p>gone</p>"), "expected_revision": made["revision"]},
        headers=auth(token),
    )
    assert read_forms(client, token, made["id"]).json() == []
    (closed,) = read_forms(client, token, made["id"], include_closed=True).json()
    assert closed["form"] == "rollout" and closed["closed_at"] is not None


def test_a_form_that_returns_after_removal_is_a_new_decision(client, db):
    token, _ = keys.mint(db, agent="ops")
    made = publish(client, token, page(ROLLOUT)).json()
    (first,) = read_forms(client, token, made["id"]).json()
    gone = client.patch(
        f"/api/v1/artifacts/{made['id']}",
        json={"body": page("<p>gone</p>"), "expected_revision": made["revision"]},
        headers=auth(token),
    ).json()
    client.patch(
        f"/api/v1/artifacts/{made['id']}",
        json={"body": page(ROLLOUT), "expected_revision": gone["revision"]},
        headers=auth(token),
    )
    (second,) = read_forms(client, token, made["id"]).json()
    assert second["decision_id"] != first["decision_id"]


def test_read_back_is_scoped_to_the_owning_key(client, db):
    token, _ = keys.mint(db, agent="ops")
    other, _ = keys.mint(db, agent="someone-else")
    made = publish(client, token, page(ROLLOUT)).json()
    not_yours = read_forms(client, other, made["id"])
    missing = read_forms(client, other, "no-such-page")
    assert not_yours.status_code == missing.status_code == 404
    assert not_yours.json() == missing.json()
    assert client.get(f"/api/v1/artifacts/{made['id']}/forms").status_code == 401


def test_controls_outside_a_form_publish_with_a_warning(client, db):
    token, _ = keys.mint(db, agent="ops")
    response = publish(client, token, page('<input name="q">'))
    assert response.status_code == 201
    shapes = [w["shape"] for w in response.json()["warnings"]]
    assert shapes == ["control_outside_form"]
    assert read_forms(client, token, response.json()["id"]).json() == []


def test_a_form_element_with_the_marker_no_longer_gets_the_csp_warning(client, db):
    token, _ = keys.mint(db, agent="ops")
    plain = publish(client, token, page("<form><p>x</p></form>")).json()
    assert [w["shape"] for w in plain["warnings"]] == ["form_element"]
    assert "data-artifact-form" in plain["warnings"][0]["hint"]
    marked = publish(client, token, page(ROLLOUT)).json()
    assert marked["warnings"] == []


def test_a_form_is_not_a_question_waiting_on_him_yet(client, db):
    token, _ = keys.mint(db, agent="ops")
    made = publish(client, token, page(ROLLOUT)).json()
    assert decisions.across_seats(db) == []
    assert decisions.open_counts(db) == (0, 0, 0)
    assert (
        client.get("/api/v1/decisions?open_only=true", headers=auth(token)).json() == []
    )
    # ... though it reads back with the page's decisions and the owner can close it.
    (form,) = read_forms(client, token, made["id"]).json()
    closed = client.post(
        f"/api/v1/decisions/{form['decision_id']}/close", headers=auth(token)
    )
    assert closed.status_code == 200
    assert read_forms(client, token, made["id"]).json() == []


def test_a_form_cannot_be_answered_or_relayed_yet(client, db):
    token, _ = keys.mint(db, agent="ops")
    made = publish(client, token, page(ROLLOUT)).json()
    (form,) = read_forms(client, token, made["id"]).json()
    response = client.post(
        f"/api/v1/decisions/{form['decision_id']}/record",
        json={"letter": "a", "source": "terminal"},
        headers=auth(token),
    )
    assert response.status_code == 422
    with pytest.raises(decisions.InvalidAnswer):
        decisions.answer(db, form["decision_id"], letter="a", by="jeremy")


def test_a_question_cannot_replace_or_wait_on_a_form(client, db):
    token, _ = keys.mint(db, agent="ops")
    made = publish(client, token, page(ROLLOUT)).json()
    (form,) = read_forms(client, token, made["id"]).json()
    options = [{"letter": "a", "label": "x"}, {"letter": "b", "label": "y"}]
    for extra in (
        {"replaces": form["decision_id"]},
        {"depends_on": [form["decision_id"]]},
    ):
        response = client.post(
            f"/api/v1/artifacts/{made['id']}/decisions",
            json={"question": "q?", "options": options, **extra},
            headers=auth(token),
        )
        assert response.status_code == 422, response.text


def test_archiving_the_page_closes_its_forms(client, db):
    token, _ = keys.mint(db, agent="ops")
    made = publish(client, token, page(ROLLOUT)).json()
    client.post(
        f"/api/v1/artifacts/{made['id']}/archive",
        json={"archived": True},
        headers=auth(token),
    )
    assert read_forms(client, token, made["id"]).json() == []


def test_a_duplicate_of_a_page_mints_its_own_forms(client, db):
    token, _ = keys.mint(db, agent="ops")
    made = publish(client, token, page(ROLLOUT)).json()
    copy = client.post(
        f"/api/v1/artifacts/{made['id']}/duplicate", headers=auth(token)
    ).json()
    (a,) = read_forms(client, token, made["id"]).json()
    (b,) = read_forms(client, token, copy["id"]).json()
    assert a["decision_id"] != b["decision_id"] and a["fields"] == b["fields"]
