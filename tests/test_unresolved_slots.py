"""A decision slot that names no decision here: said out loud, on both sides.

homelab-ij8.50. Jeremy, reading a migrated decisions page: "some questions
didn't give me the option". A `data-artifact-decision` slot whose id resolves
to nothing rendered as NOTHING, so a wrong id, a control still arriving and a
page with no question at all were the same pixels -- and the agent that wrote
the wrong id cannot see its own rendered page, so nobody caught it.

Two halves, each tested here: the publisher is told in its publish warnings,
and the reader is told by a note the page shim writes into the empty slot.
The bead's second criterion is the one that is easy to break by accident: an
ANSWERED decision still resolves, because releasing its room is correct and
must stay distinguishable from a slot that never had a question.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, keys, store
from artifacts.app import create_gallery_app
from artifacts.content import create_content_app

FRAMED = {"sec-fetch-dest": "iframe"}
OPTIONS = [{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}]


def page(*slots: str, quote: str = '"') -> str:
    lines = ["<!doctype html>", "<html><head><title>Board</title></head><body>"]
    lines += [f"<div data-artifact-decision={quote}{s}{quote}></div>" for s in slots]
    lines += ["</body></html>"]
    return "\n".join(lines)


def _ask(db, artifact_id: str) -> str:
    return decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=artifact_id,
        question="Which?",
        options=OPTIONS,
        recommended="a",
    ).id


# --- the check itself --------------------------------------------------------


def test_a_slot_naming_a_real_decision_on_this_page_resolves(db):
    art = store.publish(db, "ops-seat", "Board", page())
    real = _ask(db, art.id)
    assert decisions.unresolved_slots(db, art.id, page(real)) == []


def test_a_slot_naming_nothing_is_reported_with_its_line(db):
    art = store.publish(db, "ops-seat", "Board", page())
    real = _ask(db, art.id)
    body = page(real, "the-decision-id")
    # doctype, head, the real slot, then the placeholder on line 4
    assert decisions.unresolved_slots(db, art.id, body) == [("the-decision-id", 4)]


def test_a_decision_on_another_page_does_not_resolve_here(db):
    """The gallery fills a slot only with THIS page's decisions, so an id
    copied from a sibling page renders as nothing just like a typo."""
    here = store.publish(db, "ops-seat", "Here", page())
    there = store.publish(db, "ops-seat", "There", page())
    elsewhere = _ask(db, there.id)
    assert decisions.unresolved_slots(db, here.id, page(elsewhere)) == [(elsewhere, 3)]


def test_an_answered_decision_still_resolves(db):
    """The second acceptance criterion. Answered means its room is released,
    correctly -- it must never be told apart from a live one by being called
    missing, or every page with a settled question grows a false alarm."""
    art = store.publish(db, "ops-seat", "Board", page())
    real = _ask(db, art.id)
    decisions.answer(db, real, letter="a", by="jeremy")
    assert decisions.unresolved_slots(db, art.id, page(real)) == []


def test_a_single_quoted_slot_is_checked_too(db):
    art = store.publish(db, "ops-seat", "Board", page())
    assert decisions.unresolved_slots(db, art.id, page("nope", quote="'")) == [("nope", 3)]


def test_a_repeated_slot_is_reported_once_at_its_first_line(db):
    """Twice would invite fixing one and believing it was done."""
    art = store.publish(db, "ops-seat", "Board", page())
    assert decisions.unresolved_slots(db, art.id, page("nope", "nope")) == [("nope", 3)]


def test_a_page_with_no_slots_costs_no_query(db, monkeypatch):
    art = store.publish(db, "ops-seat", "Board", page())

    def refuse():
        raise AssertionError("queried the database for a page with no slots")

    monkeypatch.setattr(db, "connect", refuse)
    assert decisions.unresolved_slots(db, art.id, page()) == []


# --- the publisher is told ---------------------------------------------------


@pytest.fixture()
def api(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    token, _ = keys.mint(db, agent="ops-seat")
    client = TestClient(create_gallery_app(db=db))
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def test_publishing_a_dead_slot_warns_with_the_id_and_the_line(api):
    response = api.post(
        "/api/v1/artifacts", json={"title": "Board", "body": page("the-decision-id")}
    )
    assert response.status_code == 201, "advisory: the publish still succeeds"
    [warning] = response.json()["warnings"]
    assert warning["shape"] == "unresolved_decision_slot"
    assert warning["line"] == 3
    assert "'the-decision-id'" in warning["hint"]
    assert "/decisions" in warning["hint"], "and says what to do instead"


def test_the_ordinary_flow_warns_once_and_then_not_again(api):
    """Publish, ask, republish with the returned id: the only way a slot can
    be right, since a decision needs its page to exist first. The republish is
    the one that must come back clean."""
    first = api.post("/api/v1/artifacts", json={"title": "Board", "body": page()}).json()
    assert first["warnings"] == []
    asked = api.post(
        f"/api/v1/artifacts/{first['id']}/decisions",
        json={"question": "Which?", "options": OPTIONS, "recommended": "a"},
    )
    assert asked.status_code == 201, asked.text
    second = api.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={"body": page(asked.json()["id"]), "expected_revision": first["revision"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["warnings"] == []


def test_reading_the_body_back_carries_the_same_warning(api):
    """--fetch is where a seat goes to find out what is wrong with a page it
    cannot see, so the warning has to be there too, not only at publish."""
    art = api.post(
        "/api/v1/artifacts", json={"title": "Board", "body": page("nope")}
    ).json()
    read = api.get(f"/api/v1/artifacts/{art['id']}/body").json()
    assert [w["shape"] for w in read["warnings"]] == ["unresolved_decision_slot"]


# --- the reader is told ------------------------------------------------------


def _serve(db, monkeypatch, body: str, asks: int = 0) -> tuple[str, list[str]]:
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    art = store.publish(db, "ops-seat", "Board", page())
    real = [_ask(db, art.id) for _ in range(asks)]
    body = body.format(*real)
    store.patch(db, "ops-seat", art.id, body, art.revision)
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{art.id}.tenfourty.com"})
    token = store.read_token(db, art.id)
    return client.get(f"/t/{token}/", headers=FRAMED).text, real


def test_the_page_names_a_dead_slot_to_its_shim(db, monkeypatch):
    served, _ = _serve(db, monkeypatch, page("nope"))
    assert 'var gone=["nope"]' in served
    assert "Nothing has been lost" in served


def test_a_page_whose_slots_all_resolve_carries_no_note_code(db, monkeypatch):
    """Byte-identical to a page served before this existed: the note code is
    omitted, not merely handed an empty list."""
    served, _ = _serve(db, monkeypatch, page("{0}"), asks=1)
    assert "var gone=" not in served
    assert "Nothing has been lost" not in served


def test_an_id_cannot_break_out_of_the_shim(db, monkeypatch):
    """The id is the agent's own string, spliced into an inline script. A
    literal `</script>` in it would end the shim and hand the rest to the
    HTML parser."""
    served, _ = _serve(db, monkeypatch, page("x</script><b>y"))
    # Only what was INJECTED: the page's own attribute legitimately carries
    # the same characters, and asserting on the whole document would fail on
    # the fixture rather than on the shim. The shim sits between the doctype
    # and the page's <html>.
    shim = served.split("<html>", 1)[0]
    assert "var gone=" in shim
    assert "x</script>" not in shim
    assert "x\\u003c/script\\u003e\\u003cb\\u003ey" in shim


def test_an_older_version_carries_no_note(db, monkeypatch):
    """An inert snapshot gets the null shim and nothing else, as it always has."""
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    art = store.publish(db, "ops-seat", "Board", page("nope"))
    store.patch(db, "ops-seat", art.id, page(), art.revision)
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{art.id}.tenfourty.com"})
    token = store.read_token(db, art.id)
    served = client.get(f"/t/{token}/v/1", headers=FRAMED).text
    assert "var gone=" not in served
