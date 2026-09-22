"""The gallery's requests are visible; the content origin's are not.

homelab-ij8.* — 20 Sep 2026. Jeremy reported that answering a question did not
work on his phone, and nothing anywhere could say whether his request had even
arrived. Two sessions spent half an hour on competing theories that one log line
would have settled.

The constraint that made it silent is real and stays: a content-page URL carries
its read capability token in the PATH, so uvicorn runs with --no-access-log. The
mistake was turning logging off for the whole process to protect one of the two
apps it serves.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from artifacts import frame_pass, keys, serve, store
from artifacts.app import create_gallery_app
from artifacts.content import create_content_app

PAGE = "<!doctype html><html><head><title>B</title></head><body>x</body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def test_a_gallery_request_is_logged_with_its_status(client, caplog):
    with caplog.at_level(logging.INFO, logger="artifacts.gallery.access"):
        client.get("/")
    lines = [r.getMessage() for r in caplog.records]
    assert any("GET / -> 200" in line for line in lines), lines


def test_a_refused_answer_is_logged_so_a_403_is_not_silence(client, db, caplog):
    """The whole point. A form guard that refuses must leave a trace, or a
    refusal is indistinguishable from a request that never arrived — which is
    exactly the position this service was in when Jeremy said it did not work.
    """
    from artifacts import decisions

    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    asked = decisions.ask(
        db,
        agent="seat",
        artifact_id=art.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    with caplog.at_level(logging.INFO, logger="artifacts.gallery.access"):
        got = client.post(
            f"/d/{asked.id}/answer",
            data={"letter": "a", "note": ""},
            headers={"sec-fetch-site": "cross-site"},
            follow_redirects=False,
        )
    assert got.status_code == 403
    lines = [r.getMessage() for r in caplog.records]
    assert any("-> 403" in line for line in lines), lines
    assert any("sec-fetch-site=cross-site" in line for line in lines), (
        "the header the guard turns on must be in the line, or a refusal "
        "cannot be told apart from a request that never came"
    )


def test_the_content_origin_is_never_logged(db, monkeypatch, caplog):
    """A content URL carries the read capability token in its path. Logging one
    would write a secret to disk, which is why the process-wide access log is
    off and must stay off."""
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    token = frame_pass.mint(db, art.id)
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    content = TestClient(create_content_app(db))
    content.headers.update({"host": f"{art.id}.tenfourty.com"})
    with caplog.at_level(logging.INFO):
        content.get(f"/p/{token}/", headers={"sec-fetch-dest": "iframe"})

    # This service's OWN loggers only. The first version of this assertion swept
    # up every record at root INFO and caught httpx logging its request line —
    # the TEST CLIENT's chatter, not the service's. Worth keeping in mind: httpx
    # logs full URLs at INFO, so turning on root INFO logging here would put a
    # capability token in a log through any outbound httpx call. Nothing makes
    # one today, and the gallery logger does not propagate to root.
    ours = [r for r in caplog.records if r.name.startswith("artifacts.")]
    for record in ours:
        assert token not in record.getMessage(), "a capability token reached a log"

    # And positively: the content origin produced no access-log line at all,
    # rather than merely one that happened to omit the token.
    assert not [r for r in caplog.records if r.name == "artifacts.gallery.access"]


def test_healthz_is_not_logged(client, caplog):
    """Kubernetes probes it every few seconds; logging it would bury everything
    else and make the log useless for the thing it exists for."""
    with caplog.at_level(logging.INFO, logger="artifacts.gallery.access"):
        client.get("/healthz")
    assert not [r for r in caplog.records if "healthz" in r.getMessage()]


def test_the_logger_is_armed_with_a_handler_at_the_entry_point():
    """Under uvicorn's own config the root logger has NO handlers and sits at
    WARNING, so an unarmed logger emits nothing at all — the same silent-success
    shape as the bug being fixed. Measured before this was written."""
    logger = logging.getLogger("artifacts.gallery.access")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True

    serve._arm_gallery_access_log()
    assert logger.handlers, "no handler: the line would go nowhere"
    assert logger.level == logging.INFO
    assert logger.propagate is False

    before = list(logger.handlers)
    serve._arm_gallery_access_log()
    assert logger.handlers == before, "arming twice must not double every line"
