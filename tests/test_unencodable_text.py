"""Text that cannot be encoded as UTF-8 is refused with 422 on every field.

A lone surrogate (`"\\ud800"` in JSON) survives decoding, but it can be neither stored nor
sent on to the second scanner. The notes had no size check to refuse it first, so it reached
the relay, was logged as "unreachable", and then failed the write with a 500. Now each field
that is scanned refuses it before any scanner is asked, and the relay is never read as down
for it.
"""

import json
import logging

import pytest
from fastapi.testclient import TestClient
from test_ggscan import TOKEN, relay  # noqa: F401 -- fixture

from ream import ggscan, keys, store, thumbs
from ream.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>B</title></head><body>"
    "<div id='log'>first</div></body></html>"
)
PAGE2 = PAGE.replace("first", "second")
BAD = "café \ud800"  # what a JSON string "café \ud800" decodes to


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


@pytest.fixture()
def mine(db, relay):
    """A page of the seat's, and a relay that is set and has been asked nothing since."""
    token, _ = keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    relay.received.clear()
    return token, page


def auth(token):
    return {"Authorization": f"Bearer {token}", "content-type": "application/json"}


def send(client, method, path, token, extra=None, **fields):
    """The request as an agent's client puts it on the wire: JSON that escapes the lone
    surrogate, so the server decodes it back into one."""
    return client.request(
        method,
        path,
        content=json.dumps(fields).encode(),
        headers={**auth(token), **(extra or {})},
    )


def versions(db, page_id):
    with db.connect() as conn:
        return conn.execute(
            "select count(*) from version where artifact_id = %s", (page_id,)
        ).fetchone()[0]


def refused(r):
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] in ("note", "body")
    assert r.json()["detail"]["detail"].endswith("is not valid UTF-8")


# --- the three notes ----------------------------------------------------------


@pytest.mark.parametrize("extra", [None, {"Idempotency-Key": "k-1"}])
def test_a_check_note_that_cannot_be_encoded_is_a_422_and_stores_nothing(
    client, db, mine, relay, extra
):
    token, page = mine
    before = client.get(f"/api/v1/artifacts/{page.id}", headers=auth(token)).json()
    r = send(
        client,
        "POST",
        f"/api/v1/artifacts/{page.id}/touch",
        token,
        extra,
        status="failed",
        note=BAD,
    )
    refused(r)
    assert r.json()["detail"]["field"] == "note"
    after = client.get(f"/api/v1/artifacts/{page.id}", headers=auth(token)).json()
    assert after == before, "no check was recorded"
    assert relay.received == [], "nothing was sent to the relay"


def test_a_page_note_that_cannot_be_encoded_is_a_422_and_stores_nothing(
    client, db, mine, relay
):
    token, page = mine
    r = send(
        client,
        "PATCH",
        f"/api/v1/artifacts/{page.id}",
        token,
        body=PAGE2,
        expected_revision=page.revision,
        note=BAD,
    )
    refused(r)
    assert r.json()["detail"]["field"] == "note"
    assert versions(db, page.id) == 1
    assert store.get_body(db, "seat", page.id).body == PAGE
    assert relay.received == []


def test_a_section_note_that_cannot_be_encoded_is_a_422_and_stores_nothing(
    client, db, mine, relay
):
    token, page = mine
    r = send(
        client,
        "PATCH",
        f"/api/v1/artifacts/{page.id}/sections/log",
        token,
        op="append",
        html="<p>x</p>",
        note=BAD,
    )
    refused(r)
    assert r.json()["detail"]["field"] == "note"
    assert versions(db, page.id) == 1
    assert store.get_body(db, "seat", page.id).body == PAGE
    assert relay.received == []


def test_without_a_relay_a_note_that_cannot_be_encoded_is_a_422_too(
    client, db, monkeypatch
):
    token, _ = keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    for name in (ggscan.URL_ENV, ggscan.TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    r = send(
        client,
        "PATCH",
        f"/api/v1/artifacts/{page.id}",
        token,
        body=PAGE2,
        expected_revision=page.revision,
        note=BAD,
    )
    refused(r)
    assert versions(db, page.id) == 1


# --- a preview ----------------------------------------------------------------


@pytest.fixture()
def browser(monkeypatch):
    monkeypatch.setattr(thumbs, "available", lambda: True)
    monkeypatch.setattr(thumbs, "render_report", lambda h, w, ht: (None, None))


@pytest.mark.parametrize("format", ["html", "markdown"])
def test_a_preview_of_text_that_cannot_be_encoded_is_a_422(
    client, db, mine, relay, browser, format
):
    token, _ = mine
    r = send(
        client, "POST", "/api/v1/preview", token, body=f"<p>{BAD}</p>", format=format
    )
    refused(r)
    assert r.json()["detail"]["field"] == "body"
    assert relay.received == []


def test_a_preview_maps_the_stores_refusal_to_a_422_as_well(
    client, mine, browser, monkeypatch
):
    def refuse(body, *, agent):
        raise store.InvalidText("body")

    monkeypatch.setattr(store, "check_body", refuse)
    token, _ = mine
    refused(send(client, "POST", "/api/v1/preview", token, body=PAGE))


# --- the relay is not blamed ----------------------------------------------------


def test_text_that_cannot_be_encoded_is_not_read_as_an_unreachable_relay(relay, caplog):
    with caplog.at_level(logging.WARNING, logger="ream.ggscan"):
        with pytest.raises(UnicodeEncodeError):
            ggscan.scan(f"<p>{BAD}</p>")
        with pytest.raises(ggscan.UnencodableText):
            ggscan._post(relay.url, TOKEN, BAD)
    assert relay.received == [], "nothing was sent"
    assert not any("unreachable" in r.getMessage() for r in caplog.records)


def test_a_relay_that_cannot_be_reached_still_reads_as_unreachable(monkeypatch, caplog):
    # Nothing listens on port 9 (the address test_ggscan uses for "unreachable").
    monkeypatch.setenv(ggscan.URL_ENV, "http://127.0.0.1:9/scan")
    monkeypatch.setenv(ggscan.TOKEN_ENV, TOKEN)
    with caplog.at_level(logging.WARNING, logger="ream.ggscan"):
        assert ggscan.scan("<p>ok</p>") is None
    assert any("unreachable" in r.getMessage() for r in caplog.records)


def test_another_encoding_error_in_the_call_still_reads_as_unreachable(
    relay, monkeypatch, caplog
):
    """A token a header cannot carry is the relay's configuration, not the text's fault."""
    monkeypatch.setenv(ggscan.TOKEN_ENV, "tőken")
    with caplog.at_level(logging.WARNING, logger="ream.ggscan"):
        assert ggscan.scan("<p>ok</p>") is None
    assert any("unreachable" in r.getMessage() for r in caplog.records)
