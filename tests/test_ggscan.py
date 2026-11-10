"""ggshield as the second opinion on every page (homelab-ij8.2; Jeremy's ruling
of 24 Sep 2026: a finding refuses, an unreachable relay publishes and is logged,
previews are scanned too)."""

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from ream import comments, ggscan, keys, plugin_api, scanners, store, thumbs
from ream.app import create_gallery_app
from ream.scan import Finding

TOKEN = "relay-test-token"  # noqa: S105 -- a test fixture, not a credential
PAGE = "<!doctype html><title>t</title><p>caf&eacute; &#x3c;b&#x3e; ok</p>"


class Relay:
    def __init__(self):
        self.answer = {"scanned": True, "findings": []}
        self.status = 200
        self.received = []
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers["content-length"])
                relay.received.append(
                    (
                        self.headers.get("authorization"),
                        json.loads(self.rfile.read(length)),
                    )
                )
                data = json.dumps(relay.answer).encode()
                self.send_response(relay.status)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        ).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/scan"


@pytest.fixture()
def relay(monkeypatch):
    r = Relay()
    monkeypatch.setenv(ggscan.URL_ENV, r.url)
    monkeypatch.setenv(ggscan.TOKEN_ENV, TOKEN)
    yield r
    r.server.shutdown()


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def auth(db):
    token, _ = keys.mint(db, agent="seat")
    return {"Authorization": f"Bearer {token}"}


def _publish(api, auth, body=PAGE):
    return api.post(
        "/api/v1/artifacts", json={"title": "Board", "body": body}, headers=auth
    )


def test_a_ggshield_finding_refuses_the_publish_and_names_the_kind(api, auth, relay):
    relay.answer = {
        "scanned": True,
        "findings": [{"type": "Slack Bot Token", "line": 4}],
    }
    refused = _publish(api, auth)
    assert refused.status_code == 422
    assert refused.json()["detail"]["refused"] == [
        {"field": "body", "shape": "ggshield_slack_bot_token", "line": 4}
    ]


def test_ggshield_is_handed_the_original_body_with_its_token(api, auth, relay):
    """The bead's critical design point: not our unescaped view."""
    assert _publish(api, auth).status_code == 201
    presented, payload = relay.received[-1]
    assert presented == f"Bearer {TOKEN}"
    assert payload == {"body": PAGE}


def test_an_update_is_scanned_too(api, auth, relay):
    page = _publish(api, auth).json()
    relay.answer = {"scanned": True, "findings": [{"type": "AWS Keys", "line": 2}]}
    changed = api.patch(
        f"/api/v1/artifacts/{page['id']}",
        json={"body": PAGE + "<p>more</p>", "expected_revision": page["revision"]},
        headers=auth,
    )
    assert changed.status_code == 422


def test_an_unreachable_relay_publishes_and_says_so_in_the_log(
    api, auth, monkeypatch, caplog
):
    monkeypatch.setenv(ggscan.URL_ENV, "http://127.0.0.1:9/scan")
    monkeypatch.setenv(ggscan.TOKEN_ENV, TOKEN)
    logger = logging.getLogger("ream.refusals")
    logger.addHandler(caplog.handler)
    try:
        assert _publish(api, auth).status_code == 201
    finally:
        logger.removeHandler(caplog.handler)
    assert any("ggshield_unavailable" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "answer,status",
    [
        ({"scanned": False, "reason": "ggshield exited 128"}, 200),
        ({"detail": "bad token"}, 401),
        ({"scanned": True}, 200),
    ],
)
def test_a_relay_that_did_not_scan_is_never_read_as_clean_or_dirty(
    api, auth, relay, answer, status
):
    relay.answer, relay.status = answer, status
    assert ggscan.scan(PAGE) is None
    assert _publish(api, auth).status_code == 201


def test_findings_it_cannot_read_still_refuse(api, auth, relay):
    relay.answer = {"scanned": True, "findings": ["???"]}
    assert _publish(api, auth).status_code == 422


def test_a_local_finding_never_leaves_the_house(api, auth, relay):
    aws = "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzq" + "Q8Ud"
    refused = _publish(api, auth, PAGE.replace("ok", "AKIAIOSFODNN7QWERTYU " + aws))
    assert refused.status_code == 422
    assert relay.received == [], "an obvious secret is refused before any send"


def test_a_preview_is_scanned_too(api, auth, relay, monkeypatch):
    monkeypatch.setattr(thumbs, "available", lambda: True)
    monkeypatch.setattr(thumbs, "render_report", lambda h, w, ht: (None, None))
    relay.answer = {
        "scanned": True,
        "findings": [{"type": "Generic Password", "line": 1}],
    }
    got = api.post("/api/v1/preview", json={"body": PAGE}, headers=auth)
    assert got.status_code == 422


def test_not_configured_means_the_built_in_scan_only(api, auth, monkeypatch):
    monkeypatch.delenv(ggscan.URL_ENV, raising=False)
    monkeypatch.delenv(ggscan.TOKEN_ENV, raising=False)
    assert ggscan.scan(PAGE) is None
    assert _publish(api, auth).status_code == 201


def test_a_relay_that_swallows_packets_costs_seconds_not_the_scan_timeout(
    monkeypatch,
):
    """baba down, or a firewall rule missing: a DROP, not a refusal. Every
    publish would otherwise wait the full scan timeout."""
    import time

    # TEST-NET-1 (RFC 5737): routable nowhere, so a SYN goes unanswered.
    monkeypatch.setenv(ggscan.URL_ENV, "http://192.0.2.1:9098/scan")
    monkeypatch.setenv(ggscan.TOKEN_ENV, TOKEN)
    # The production connect bound is seconds; a short one proves the same
    # thing -- the connect bound, not the 20 s scan timeout, is what is paid.
    monkeypatch.setattr(ggscan, "CONNECT_SECONDS", 0.2)
    started = time.monotonic()
    assert ggscan.scan(PAGE) is None
    assert time.monotonic() - started < ggscan.CONNECT_SECONDS + 2


# ---- already scanned clean (runcove-5t3oj.35, 20 Oct 2026) ----------------


def test_a_body_scanned_clean_is_not_sent_again(relay):
    assert ggscan.scan(PAGE) == []
    assert ggscan.scan(PAGE) == []
    assert len(relay.received) == 1


def test_a_different_body_is_still_scanned(relay):
    ggscan.scan(PAGE)
    ggscan.scan(PAGE + "<p>more</p>")
    assert len(relay.received) == 2


def test_a_finding_is_never_remembered(relay):
    relay.answer = {"scanned": True, "findings": [{"type": "Slack Token", "line": 1}]}
    assert ggscan.scan(PAGE)
    assert ggscan.scan(PAGE)
    assert len(relay.received) == 2


def test_could_not_scan_is_never_remembered_as_clean(relay):
    relay.answer = {"scanned": False, "reason": "busy"}
    assert ggscan.scan(PAGE) is None
    relay.answer = {"scanned": True, "findings": [{"type": "Slack Token", "line": 1}]}
    assert ggscan.scan(PAGE)


def test_a_clean_answer_goes_stale_after_a_day(relay, monkeypatch):
    ggscan.scan(PAGE)
    later = ggscan.time.monotonic() + ggscan.CLEAN_FRESH_SECONDS + 1
    monkeypatch.setattr(ggscan.time, "monotonic", lambda: later)
    ggscan.scan(PAGE)
    assert len(relay.received) == 2


def test_the_memory_is_bounded_and_holds_no_text(relay, monkeypatch):
    monkeypatch.setattr(ggscan, "CLEAN_MAX", 3)
    for n in range(5):
        ggscan.scan(f"{PAGE}<p>{n}</p>")
    assert len(ggscan._clean) == 3
    assert all(len(k) == 64 for k in ggscan._clean)


# STANDING DOWN (core-and-plugins design, step 9c): the relay scan moves into the house
# plug-in as the scanner `gitguardian`. While a scanner of that name is registered, core's
# own relay scan stays out of the way, so each body goes to the relay once, not twice.
AWS_FINDING = {"scanned": True, "findings": [{"type": "AWS Keys", "line": 2}]}


@pytest.fixture()
def house(monkeypatch):
    monkeypatch.setattr(scanners, "_SCANNERS", [])
    monkeypatch.setattr(plugin_api, "_failures", {})
    return plugin_api.PluginAPI("house")


def _boom(text):
    raise RuntimeError("down")


def _stamped(db, comment_id):
    with db.connect() as conn:
        return conn.execute(
            "select gg_scanned_at is not null from comment where id = %s",
            (comment_id,),
        ).fetchone()[0]


def _agent_comment(db, page, body="fine"):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body=body,
    )


def test_without_a_plugin_the_relay_is_asked_as_ever(db, relay, house):
    assert not ggscan.stood_down()
    assert ggscan.configured()
    store.publish(db, "seat", "t", PAGE)
    assert [payload for _, payload in relay.received] == [{"body": PAGE}]


def test_a_scanner_named_otherwise_does_not_stand_the_relay_down(db, relay, house):
    seen = []
    house.scanner("acme-secrets", lambda text: seen.append(text) or [])
    assert not ggscan.stood_down()
    assert ggscan.configured()
    store.publish(db, "seat", "t", PAGE)
    assert [payload for _, payload in relay.received] == [{"body": PAGE}]
    assert seen == [PAGE]


def test_a_gitguardian_scanner_stands_the_relay_down(db, relay, house):
    seen = []
    house.scanner("gitguardian", lambda text: seen.append(text) or [])
    assert ggscan.stood_down()
    assert ggscan.scan(PAGE) is None
    # "the relay is set" is a separate question from "core asks it itself": the relay is
    # still set, and a relay scan is still expected (the plug-in's).
    assert ggscan.configured()
    assert ggscan.relay_expected()
    store.publish(db, "seat", "t", PAGE)
    assert relay.received == []
    assert seen == [PAGE]


def test_a_relay_scan_is_expected_when_the_relay_is_set_or_the_plugin_has_it(
    house, monkeypatch
):
    monkeypatch.delenv(ggscan.URL_ENV, raising=False)
    monkeypatch.delenv(ggscan.TOKEN_ENV, raising=False)
    assert not ggscan.configured()
    assert not ggscan.relay_expected()  # nothing to ask, nobody to ask it
    house.scanner("acme-secrets", lambda text: [])
    assert not ggscan.relay_expected()  # another name is no relay
    house.scanner("gitguardian", lambda text: [])
    assert not ggscan.configured()  # core's own settings say nothing of the relay
    assert ggscan.relay_expected()  # but the plug-in is expected to have scanned


def test_the_plugin_verdict_decides_a_publish_not_the_relay(db, relay, house):
    relay.answer = AWS_FINDING
    house.scanner("gitguardian", lambda text: [])
    store.publish(db, "seat", "t", PAGE)  # the relay would have refused it
    assert relay.received == []


def test_a_plugin_finding_refuses_a_publish(db, relay, house):
    house.scanner("gitguardian", lambda text: [Finding("ggshield_aws_keys", 3)])
    with pytest.raises(store.SecretFound) as caught:
        store.publish(db, "seat", "t", PAGE)
    assert [f.shape for f in caught.value.findings] == ["ggshield_aws_keys"]
    assert relay.received == []


def test_a_gitguardian_scanner_that_fails_follows_its_own_on_error(db, relay, house):
    house.scanner("gitguardian", _boom, on_error="skip")
    store.publish(db, "seat", "t", PAGE)  # skip: the page publishes on the rest
    assert plugin_api.failure_counts() == {"house:scanner": 1}
    assert relay.received == []


def test_a_blocking_gitguardian_scanner_that_fails_fails_the_publish(db, relay, house):
    house.scanner("gitguardian", _boom, on_error="block")
    with pytest.raises(store.ScanFailed):
        store.publish(db, "seat", "t", PAGE)
    assert relay.received == []


def test_a_gitguardian_field_stands_down_too(db, relay, house):
    seen = []
    house.scanner("gitguardian", lambda text: seen.append(text) or [])
    store._check_secrets(
        "a note", "note", agent="seat", artifact_id=None, gitguardian=True
    )
    assert seen == ["a note"]
    assert relay.received == []


def test_the_relay_is_asked_again_once_the_scanner_is_gone(db, relay, house):
    house.scanner("gitguardian", lambda text: [])
    store.publish(db, "seat", "t", PAGE)
    assert relay.received == []
    scanners._SCANNERS[:] = []
    assert not ggscan.stood_down()
    relay.answer = AWS_FINDING
    with pytest.raises(store.SecretFound):
        store.publish(db, "seat", "t", PAGE + "<p>more</p>")
    assert len(relay.received) == 1


def test_a_comments_second_scan_asks_the_plugin_not_the_relay(db, relay, house):
    relay.answer = AWS_FINDING  # would withdraw it, if it were asked
    house.scanner("gitguardian", lambda text: [])
    page = store.publish(db, "seat", "t", PAGE)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "scanned"
    assert _stamped(db, row.id)
    assert relay.received == []


def test_a_plugin_finding_withdraws_a_comment_and_the_relay_is_not_asked(
    db, relay, house
):
    page = store.publish(db, "seat", "t", PAGE)
    relay.received.clear()
    house.scanner("gitguardian", lambda text: [Finding("ggshield_aws_keys", 1)])
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "withdrawn"
    assert comments.list_for_artifact(db, page.id) == []
    assert relay.received == []


def test_a_gitguardian_scanner_with_no_answer_leaves_the_comment_unscanned(
    db, relay, house
):
    house.scanner("gitguardian", _boom, on_error="skip")
    page = store.publish(db, "seat", "t", PAGE)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "unavailable"
    assert not _stamped(db, row.id)  # the sweeper asks again
    assert relay.received == []


def _clean(text):
    return []


def _hang(text):
    time.sleep(2)
    return []


@pytest.fixture()
def no_relay_set(monkeypatch):
    """Core's own settings carry no relay (the plug-in reads its own configuration)."""
    monkeypatch.delenv(ggscan.URL_ENV, raising=False)
    monkeypatch.delenv(ggscan.TOKEN_ENV, raising=False)


@pytest.mark.parametrize("fails", ["raises", "times out"])
@pytest.mark.parametrize("relay_set", [True, False])
def test_another_scanners_clean_answer_is_not_the_relays_while_stood_down(
    db, house, monkeypatch, fails, relay_set
):
    """The relay scan is the `gitguardian` scanner's. When it gives no opinion the comment
    is exactly as when today's relay is unreachable: unscanned, for the sweeper to retry.
    A clean answer from some other scanner must not stand in for it."""
    if relay_set:
        monkeypatch.setenv(ggscan.URL_ENV, "http://127.0.0.1:9/scan")
        monkeypatch.setenv(ggscan.TOKEN_ENV, TOKEN)
    else:
        monkeypatch.delenv(ggscan.URL_ENV, raising=False)
        monkeypatch.delenv(ggscan.TOKEN_ENV, raising=False)
    page = store.publish(db, "seat", "t", PAGE)
    if fails == "raises":
        house.scanner("gitguardian", _boom, on_error="skip")
    else:
        house.scanner("gitguardian", _hang, on_error="skip", timeout=0.05)
    house.scanner("acme-secrets", _clean)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "unavailable"
    assert not _stamped(db, row.id)
    assert comments.list_for_artifact(db, page.id) != []  # skip: still shown
    assert plugin_api.failure_counts() == {"house:scanner": 1}


def test_without_gitguardian_another_scanners_clean_answer_still_counts_when_no_relay_is_set(
    db, house, no_relay_set
):
    """The unchanged path the case above must differ from."""
    house.scanner("acme-secrets", _clean)
    page = store.publish(db, "seat", "t", PAGE)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "scanned"
    assert _stamped(db, row.id)


def test_a_clean_gitguardian_answer_clears_a_comment_though_core_has_no_relay_set(
    db, house, no_relay_set
):
    house.scanner("gitguardian", _clean)
    page = store.publish(db, "seat", "t", PAGE)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "scanned"
    assert _stamped(db, row.id)


def test_a_clean_gitguardian_answer_needs_no_other_scanner(db, house, no_relay_set):
    house.scanner("gitguardian", _clean)
    house.scanner("acme-secrets", _boom, on_error="skip")  # the other one is down
    page = store.publish(db, "seat", "t", PAGE)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "scanned"


def test_another_scanners_finding_still_withdraws_while_gitguardian_is_down(
    db, house, no_relay_set
):
    page = store.publish(db, "seat", "t", PAGE)
    house.scanner("gitguardian", _boom, on_error="skip")
    # a finding is a finding, whoever made it
    house.scanner("acme-secrets", lambda text: [Finding("acme_token", 1)])
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "withdrawn"


def test_a_blocking_gitguardian_scanner_that_fails_holds_the_comment(
    db, house, no_relay_set
):
    page = store.publish(db, "seat", "t", PAGE)
    house.scanner("gitguardian", _boom, on_error="block")
    house.scanner("acme-secrets", _clean)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "unavailable"
    assert not _stamped(db, row.id)
    assert comments.list_for_artifact(db, page.id) == []  # held, reversibly


def test_a_stood_down_publish_does_not_blame_the_relay_when_the_plugin_is_down(
    db, house, caplog, monkeypatch
):
    """Core's `ggshield_unavailable` line says core's own relay was asked and did not
    answer. While stood down it was not asked; the plug-in counts and logs its own fault."""
    monkeypatch.setenv(ggscan.URL_ENV, "http://127.0.0.1:9/scan")
    monkeypatch.setenv(ggscan.TOKEN_ENV, TOKEN)
    house.scanner("gitguardian", _boom, on_error="skip")
    logger = logging.getLogger("ream.refusals")
    logger.addHandler(caplog.handler)
    try:
        store.publish(db, "seat", "t", PAGE)
    finally:
        logger.removeHandler(caplog.handler)
    assert not any("ggshield_unavailable" in r.getMessage() for r in caplog.records)


def test_without_a_plugin_a_comments_second_scan_asks_the_relay(db, relay):
    page = store.publish(db, "seat", "t", PAGE)
    relay.received.clear()
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "scanned"
    assert len(relay.received) == 1
