import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"

_OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


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


def _ask(client, token: str, artifact_id: str, **overrides) -> dict:
    payload = {"question": "ship it?", "options": _OPTIONS, **overrides}
    response = client.post(
        f"/api/v1/artifacts/{artifact_id}/decisions", json=payload, headers=auth(token)
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# _validate_options -- unit level, so every rejected shape is covered directly,
# mirroring comments.py's own test_each_rejected_anchor_shape_names_only_anchor.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        pytest.param([{"letter": "a", "label": "yes"}], id="one-option"),
        pytest.param(
            [{"letter": c, "label": c} for c in "abcdefg"], id="seven-options"
        ),
        pytest.param(
            [{"letter": "a", "label": "x"}, {"letter": "a", "label": "y"}],
            id="duplicate-letters",
        ),
        pytest.param(
            [{"letter": "1", "label": "x"}, {"letter": "b", "label": "y"}],
            id="a-non-letter",
        ),
        pytest.param(
            [{"letter": "A", "label": "x"}, {"letter": "b", "label": "y"}],
            id="uppercase-is-not-lowercase-ascii",
        ),
        pytest.param(
            [{"letter": "a", "label": ""}, {"letter": "b", "label": "y"}],
            id="empty-label",
        ),
        pytest.param(
            [
                {"letter": "a", "label": "x" * 201},
                {"letter": "b", "label": "y"},
            ],
            id="over-long-label",
        ),
        pytest.param(
            [{"letter": "a"}, {"letter": "b", "label": "y"}], id="missing-label-key"
        ),
        pytest.param(
            [
                {"letter": "a", "label": "x", "extra": "nope"},
                {"letter": "b", "label": "y"},
            ],
            id="extra-key",
        ),
        pytest.param("not-a-list", id="a-non-list"),
        pytest.param(["a", "b"], id="a-list-of-strings"),
        pytest.param(
            # A literal `\ud800` escape in a non-raw string literal is a real lone
            # surrogate in memory (see test_gallery.py's own note on this) -- it
            # survives JSON decoding but neither `utf-8` nor Postgres `text` can
            # hold one. This is the inherited gap named in the nudge-transport
            # brief: nothing here named a lone surrogate in an option label before.
            [{"letter": "a", "label": "bad\ud800label"}, {"letter": "b", "label": "y"}],
            id="lone-surrogate-in-label",
        ),
    ],
)
def test_each_rejected_options_shape_names_only_options(options):
    with pytest.raises(decisions.InvalidOptions) as exc_info:
        decisions._validate_options(options)
    assert exc_info.value.field == "options"


def test_each_accepted_options_shape_round_trips():
    validated, letters = decisions._validate_options(_OPTIONS)
    assert validated == _OPTIONS
    assert letters == {"a", "b"}


def test_exactly_six_options_is_the_accepted_boundary_seven_is_refused():
    """MAX_OPTIONS is 6; `test_each_rejected_options_shape_names_only_options`
    already covers one option and seven, but nothing before this named the
    boundary itself -- the inherited gap the nudge-transport brief calls out.
    A future off-by-one in the cap (`<=` becoming `<`, or vice versa) must fail
    a test whose name says exactly what it protects."""
    six = [{"letter": c, "label": c} for c in "abcdef"]
    validated, letters = decisions._validate_options(six)
    assert len(validated) == decisions.MAX_OPTIONS == 6
    assert letters == set("abcdef")

    seven = [{"letter": c, "label": c} for c in "abcdefg"]
    with pytest.raises(decisions.InvalidOptions):
        decisions._validate_options(seven)


def test_a_bad_options_shape_over_the_api_never_echoes_the_callers_content(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    marker = "UNIQUE_MARKER_should_never_be_echoed"
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/decisions",
        json={"question": "q?", "options": [marker, marker]},
        headers=auth(token),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "options"
    assert marker not in response.text


def test_recommended_not_among_the_letters_is_refused(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/decisions",
        json={"question": "q?", "options": _OPTIONS, "recommended": "z"},
        headers=auth(token),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "options"


# ---------------------------------------------------------------------------
# Round trip through the agent API.
# ---------------------------------------------------------------------------


def test_a_decision_round_trips_through_the_agent_api(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    created = _ask(client, token, page["id"], recommended="a")

    assert created["question"] == "ship it?"
    assert created["options"] == _OPTIONS
    assert created["recommended"] == "a"
    assert created["answer"] is None
    assert created["answer_note"] == ""
    assert created["answered_at"] is None
    assert created["version_n"] == page["version"]

    listed = client.get(
        f"/api/v1/artifacts/{page['id']}/decisions", headers=auth(token)
    ).json()
    assert [row["id"] for row in listed] == [created["id"]]

    listed_all = client.get("/api/v1/decisions", headers=auth(token)).json()
    assert [row["id"] for row in listed_all] == [created["id"]]


# ---------------------------------------------------------------------------
# Size caps -- question travels store's own guard (413); a label's cap is an
# InvalidOptions concern (422), never a second, differently-shaped 413.
# ---------------------------------------------------------------------------


def test_an_over_long_question_is_refused_with_413(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    question = "a" * (decisions.MAX_QUESTION_BYTES + 1)
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/decisions",
        json={"question": question, "options": _OPTIONS},
        headers=auth(token),
    )
    assert response.status_code == 413
    assert response.json()["detail"]["field"] == "question"


def test_an_over_long_note_is_refused_with_413(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    created = _ask(client, token, page["id"])
    note = "a" * (decisions.MAX_NOTE_BYTES + 1)
    with pytest.raises(store.BodyTooLarge) as exc_info:
        decisions.answer(db, created["id"], letter="a", note=note, by="jeremy")
    assert exc_info.value.field == "note"


# ---------------------------------------------------------------------------
# Secrets -- a question, a label and Jeremy's own note all get the same scan.
# ---------------------------------------------------------------------------


def test_a_secret_in_the_question_is_refused_422(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/decisions",
        json={"question": f"rotate this: {secret}?", "options": _OPTIONS},
        headers=auth(token),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == {
        "refused": [{"field": "question", "shape": "aws_access_key_id", "line": 1}]
    }
    assert secret not in response.text


def test_a_secret_in_a_label_is_also_refused(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.post(
        f"/api/v1/artifacts/{page['id']}/decisions",
        json={
            "question": "fine?",
            "options": [
                {"letter": "a", "label": secret},
                {"letter": "b", "label": "no"},
            ],
        },
        headers=auth(token),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["refused"][0]["field"] == "options"
    assert secret not in response.text


def test_a_secret_in_jeremys_note_is_refused_exactly_like_an_agents_text(client, db):
    """Trusting the human is the bug -- Jeremy's own note goes through the same
    scanner as anything an agent writes."""
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    created = _ask(client, token, page["id"])
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    with pytest.raises(store.SecretFound) as exc_info:
        decisions.answer(
            db, created["id"], letter="a", note=f"rotate {secret}", by="jeremy"
        )
    assert exc_info.value.field == "note"


# ---------------------------------------------------------------------------
# Ownership -- another agent's artifact is 404 everywhere, never a hint.
# ---------------------------------------------------------------------------


def test_another_agents_artifact_is_404_on_ask_list_and_close(client, db):
    token_a, _ = keys.mint(db, agent="seat-a")
    token_b, _ = keys.mint(db, agent="seat-b")
    page = _publish(client, token_a)
    mine = _ask(client, token_a, page["id"])
    store.set_agents_can_read(
        db, page["id"], False, agent=None
    )  # closed to other agents: homelab-ij8.105 made pages readable by default

    sneaky = client.post(
        f"/api/v1/artifacts/{page['id']}/decisions",
        json={"question": "sneaky?", "options": _OPTIONS},
        headers=auth(token_b),
    )
    assert sneaky.status_code == 404

    assert (
        client.get(
            f"/api/v1/artifacts/{page['id']}/decisions", headers=auth(token_b)
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/decisions/{mine['id']}/close", headers=auth(token_b)
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Answering -- the gallery acting for Jeremy. No agent-facing HTTP route exists
# for this (see decisions-brief.md section 4), so these exercise the store
# function directly; gallery-level guard tests live in test_gallery.py.
# ---------------------------------------------------------------------------


def test_the_answer_round_trip_is_visible_over_the_agent_api(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    created = _ask(client, token, page["id"])

    answered = decisions.answer(
        db, created["id"], letter="a", note="go ahead", by="jeremy"
    )
    assert answered.answer == "a"
    assert answered.answer_note == "go ahead"
    assert answered.answered_at is not None
    assert answered.answered_by == "jeremy"

    # An agent asking "what's still open" must not see something already
    # answered -- but it has to ASK for that now (homelab-ij8.33). This test
    # asserted the opposite until 21 Sep, when the default was flipped: a
    # plain read that hid the answer read as the answer having gone nowhere.
    still_open = client.get(
        "/api/v1/decisions?open_only=true", headers=auth(token)
    ).json()
    assert still_open == []

    for where in ("/api/v1/decisions", "/api/v1/decisions?include_answered=true"):
        with_answer = client.get(where, headers=auth(token)).json()
        assert [row["id"] for row in with_answer] == [created["id"]], where
        assert with_answer[0]["answer"] == "a"


def test_reanswering_replaces_the_answer_and_moves_answered_at(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    created = _ask(client, token, page["id"])

    first = decisions.answer(db, created["id"], letter="a", by="jeremy")
    second = decisions.answer(
        db, created["id"], letter="b", note="changed my mind", by="jeremy"
    )

    assert second.answer == "b"
    assert second.answer_note == "changed my mind"
    assert second.answered_at >= first.answered_at


def test_answering_a_closed_decision_is_not_found(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    created = _ask(client, token, page["id"])
    close = client.post(f"/api/v1/decisions/{created['id']}/close", headers=auth(token))
    assert close.status_code == 200

    with pytest.raises(store.NotFound):
        decisions.answer(db, created["id"], letter="a", by="jeremy")


def test_answering_an_unknown_letter_is_invalid_answer(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    created = _ask(client, token, page["id"])

    with pytest.raises(decisions.InvalidAnswer) as exc_info:
        decisions.answer(db, created["id"], letter="z", by="jeremy")
    assert exc_info.value.field == "answer"


def test_close_is_idempotent(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    created = _ask(client, token, page["id"])

    first = client.post(f"/api/v1/decisions/{created['id']}/close", headers=auth(token))
    assert first.status_code == 200
    assert first.json()["closed_at"] is not None

    second = client.post(
        f"/api/v1/decisions/{created['id']}/close", headers=auth(token)
    )
    assert second.status_code == 200
    assert second.json()["closed_at"] == first.json()["closed_at"]


def test_list_for_agent_never_returns_a_closed_decision_even_with_include_answered(
    client, db
):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    created = _ask(client, token, page["id"])
    decisions.answer(db, created["id"], letter="a", by="jeremy")
    client.post(f"/api/v1/decisions/{created['id']}/close", headers=auth(token))

    listed = client.get(
        "/api/v1/decisions?include_answered=true", headers=auth(token)
    ).json()
    assert listed == []


def test_the_answer_event_is_addressed_to_the_agent_that_owns_the_page(client, db):
    """The join's columns are read from the end of the row, and this is why.

    `answer` selects the decision's own columns plus two from the joined artifact,
    and for a while it read those two by number -- the numbers that happened to
    follow a twelve-column list. Adding two columns to that list (19 Sep 2026, the
    acknowledgement pair) moved them, so the owning agent silently became None and
    every answer tried to write an event addressed to nobody. A NOT NULL constraint
    caught it that day; this test is what catches it when the shape changes again
    somewhere the database is more forgiving.
    """
    from artifacts import events

    token, _ = keys.mint(db, agent="page-owner")
    artifact = _publish(client, token)
    created = _ask(client, token, artifact["id"])

    decisions.answer(db, created["id"], letter="a", by="jeremy")

    rows = events.list_for_agent(db, "page-owner", since=0, limit=10)
    answered = [row for row in rows if row.kind == "decision_answered"]
    assert len(answered) == 1
    assert answered[0].artifact_id == artifact["id"]


# --- what a plain read returns ------------------------------------------------
#
# homelab-ij8.33. Both decision reads used to hide an answered decision, so an
# agent reading back after Jeremy had answered got an empty list -- which reads
# exactly like "the answers went nowhere". The first outside agent to use this
# service was about to conclude answers were write-only on the strength of it.


def _answer(db, decision_id: str, letter: str = "a", note: str = "") -> None:
    decisions.answer(db, decision_id, letter=letter, note=note, by="jeremy")


def test_a_plain_read_of_a_page_returns_his_answer(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    asked = _ask(client, token, page["id"])
    _answer(db, asked["id"], "b", "but only after the backups move")

    [row] = client.get(
        f"/api/v1/artifacts/{page['id']}/decisions", headers=auth(token)
    ).json()
    assert row["answer"] == "b"
    assert row["answer_note"] == "but only after the backups move", (
        "his words come back with it -- the note is usually what qualifies the letter"
    )


def test_a_plain_read_across_pages_returns_his_answer(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    asked = _ask(client, token, page["id"])
    _answer(db, asked["id"])
    rows = client.get("/api/v1/decisions", headers=auth(token)).json()
    assert [r["answer"] for r in rows] == ["a"]


def test_open_only_is_the_explicit_ask(client, db):
    """ "What still needs him" is a real question; it is just not the common
    one, so it says so at the call site now."""
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    settled = _ask(client, token, page["id"], question="settled?")
    _ask(client, token, page["id"], question="still open?")
    _answer(db, settled["id"])

    rows = client.get(
        f"/api/v1/artifacts/{page['id']}/decisions?open_only=true", headers=auth(token)
    ).json()
    assert [r["question"] for r in rows] == ["still open?"]
    across = client.get("/api/v1/decisions?open_only=true", headers=auth(token)).json()
    assert [r["question"] for r in across] == ["still open?"]


def test_the_old_spelling_still_means_what_it_meant(client, db):
    """Every caller that existed had already learned to pass
    include_answered=true, and two of them are in another repo on another
    seat's schedule. Changing a default must not break them either way."""
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    settled = _ask(client, token, page["id"], question="settled?")
    _ask(client, token, page["id"], question="still open?")
    _answer(db, settled["id"])

    both = client.get(
        f"/api/v1/artifacts/{page['id']}/decisions?include_answered=true",
        headers=auth(token),
    ).json()
    assert len(both) == 2
    open_only = client.get(
        f"/api/v1/artifacts/{page['id']}/decisions?include_answered=false",
        headers=auth(token),
    ).json()
    assert [r["question"] for r in open_only] == ["still open?"]


def test_a_withdrawn_decision_still_needs_asking_for(client, db):
    """Flipping one default must not quietly flip the other: a withdrawn
    question is a different thing from an answered one, and ij8.44's route in
    to it is still explicit."""
    token, _ = keys.mint(db, agent="ops-seat")
    page = _publish(client, token)
    asked = _ask(client, token, page["id"])
    decisions.close(db, "ops-seat", asked["id"])

    assert (
        client.get(
            f"/api/v1/artifacts/{page['id']}/decisions", headers=auth(token)
        ).json()
        == []
    )
    with_closed = client.get(
        f"/api/v1/artifacts/{page['id']}/decisions?include_closed=true",
        headers=auth(token),
    ).json()
    assert len(with_closed) == 1


def test_get_owned_returns_own_row_and_none_for_others_or_missing(db):
    page = store.publish(db, "seat", "Board", PAGE)
    row = decisions.ask(
        db, agent="seat", artifact_id=page.id, question="q?", options=_OPTIONS
    )
    assert decisions.get_owned(db, "seat", row.id).id == row.id
    assert decisions.get_owned(db, "other", row.id) is None
    assert decisions.get_owned(db, "seat", "nope") is None
