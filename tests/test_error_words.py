"""The words an API error uses for the owner follow the settings (design 3.4, slice 3), and a
caller's own text in an error is never expanded as a template."""

from test_sections import _ask, client, page, section  # noqa: F401 -- fixtures

from artifacts import decisions, keys, review, settings, store

PAGE = "<!doctype html><html><head><title>B</title></head><body>x</body></html>"
TWO = [{"letter": "a", "label": "x"}, {"letter": "b", "label": "y"}]


def _page_source_error(client, db, asked):
    token, _ = keys.mint(db, agent="ops")
    got = client.post(
        f"/api/v1/decisions/{asked.id}/record",
        json={"letter": "a", "note": "x", "source": "page"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert got.status_code == 422
    return got.json()["detail"]


def test_an_error_follows_the_owner_settings(client, db, page):
    asked = _ask(db, page[0])
    assert _page_source_error(client, db, asked).endswith(
        "it means Jeremy answered on the page himself"
    )
    with settings.override(owner_name="Ann", owner_pronoun="she"):
        detail = _page_source_error(client, db, asked)
    assert detail.endswith("it means Ann answered on the page herself")
    assert "Jeremy" not in detail


def test_a_review_message_follows_the_owner_settings(db):
    art = store.publish(db, "seat", "Plan", PAGE)
    with settings.override(owner_name="Ann", owner_pronoun="she"):
        row = decisions.ask_review(db, agent="seat", artifact_id=art.id)
        decisions.answer(db, row.id, letter="c", note="", by="jeremy")
        message, _ = review.render(db, row.id, agent="seat")
    assert "She did not say what to change: ask her" in message
    assert "She left no marks." in message
    assert " he " not in message and "He " not in message


def test_a_callers_text_quoted_in_an_error_is_not_expanded(client, db, page):
    aid, token = page
    asked = decisions.ask(
        db,
        agent="ci-seat",
        artifact_id=aid,
        question="Will {{owner_name}} {{nonsense}} ship it?",
        options=TWO,
    )
    slot = f'<div data-artifact-decision="{asked.id}"></div>'
    assert section(client, aid, token, "log", op="append", html=slot).is_success
    r = section(client, aid, token, "log", op="replace", html="gone")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]["detail"]
    assert "Will {{owner_name}} {{nonsense}} ship it?" in detail
    assert "which he has not answered" in detail
    with settings.override(owner_name="Ann", owner_pronoun="she"):
        again = section(client, aid, token, "log", op="replace", html="gone")
    assert "which she has not answered" in again.json()["detail"]["detail"]
