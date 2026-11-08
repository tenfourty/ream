"""Failed agent keys are counted per address, and a burst earns a refusal that
says so in words -- homelab-ij8.100, before the agent API goes on the internet.

The rule every test here leans on: a key that WORKS is never refused. The
internal ingress may hand every LAN caller to the app from one node address, so
blocking an address outright would let one broken agent lock out the house.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from ream import keys, ratelimit
from ream.app import create_app, create_gallery_app

GALLERY_HOST = "artifacts.tenfourty.site"
API_HOST = "agents.example.test"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limiter(clock: Clock, **kw) -> ratelimit.FailedAuthLimiter:
    return ratelimit.FailedAuthLimiter(
        threshold=kw.pop("threshold", 3), window=60, cooldown=300, clock=clock, **kw
    )


# --- the limiter on its own -------------------------------------------------


def test_failures_below_the_threshold_do_not_block():
    clock = Clock()
    limiter = _limiter(clock)
    assert limiter.failed("1.2.3.4") == (1, False, False)
    assert limiter.failed("1.2.3.4") == (2, False, False)
    assert limiter.retry_after("1.2.3.4") == 0


def test_the_threshold_failure_blocks_once_and_says_it_was_the_one():
    clock = Clock()
    limiter = _limiter(clock)
    limiter.failed("1.2.3.4")
    limiter.failed("1.2.3.4")
    assert limiter.failed("1.2.3.4") == (3, True, True)
    # Further failures while blocked are blocked, but not "newly": the log line
    # is written once per cooldown, not once per request.
    assert limiter.failed("1.2.3.4") == (4, True, False)
    assert limiter.retry_after("1.2.3.4") == pytest.approx(300)


def test_failures_age_out_of_the_window():
    clock = Clock()
    limiter = _limiter(clock)
    limiter.failed("1.2.3.4")
    limiter.failed("1.2.3.4")
    clock.now += 61
    assert limiter.failed("1.2.3.4") == (1, False, False)


def test_the_cooldown_ends():
    clock = Clock()
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.failed("1.2.3.4")
    clock.now += 301
    assert limiter.retry_after("1.2.3.4") == 0


def test_addresses_are_counted_separately():
    clock = Clock()
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.failed("1.2.3.4")
    assert limiter.retry_after("5.6.7.8") == 0
    assert limiter.failed("5.6.7.8") == (1, False, False)


def test_memory_is_bounded_and_the_quietest_caller_is_forgotten_first():
    clock = Clock()
    limiter = _limiter(clock, max_tracked=2)
    limiter.failed("a")
    limiter.failed("b")
    limiter.failed("a")  # a is now the most recent
    limiter.failed("c")  # evicts b, not a
    assert limiter.failed("a")[0] == 3
    assert limiter.failed("b")[0] == 1


def test_the_address_is_the_one_nginx_sets_never_forwarded_for():
    # nginx overwrites X-Real-IP and only APPENDS to X-Forwarded-For, so the
    # latter's first entry is whatever the caller wrote.
    headers = {"x-real-ip": "203.0.113.9", "x-forwarded-for": "10.0.0.1"}
    assert ratelimit.client_address(headers, "10.244.1.5") == "203.0.113.9"
    assert ratelimit.client_address({"x-forwarded-for": "10.0.0.1"}, "10.244.1.5") == (
        "10.244.1.5"
    )
    assert ratelimit.client_address({}, None) == "unknown"


# --- through the real routes ------------------------------------------------


@pytest.fixture()
def auth_log(caplog):
    """The auth logger does not propagate once `serve` arms it, so caplog's
    root handler would miss it. Attach directly."""
    logger = logging.getLogger("ream.auth")
    propagate = logger.propagate
    # Not propagating while attached, or an unarmed logger would reach caplog
    # twice -- directly and through the root -- and every count would double.
    logger.propagate = False
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger="ream.auth")
    yield caplog
    logger.removeHandler(caplog.handler)
    logger.propagate = propagate


def _client(db, threshold=3):
    limiter = ratelimit.FailedAuthLimiter(threshold=threshold, window=60, cooldown=300)
    return TestClient(create_gallery_app(db, limiter))


def _bad(client, address="203.0.113.9"):
    return client.get(
        "/api/v1/artifacts",
        headers={"Authorization": "Bearer art_nope", "x-real-ip": address},
    )


def test_a_burst_of_bad_keys_is_refused_with_429_and_words(db):
    client = _client(db)
    assert [_bad(client).status_code for _ in range(2)] == [401, 401]
    refused = _bad(client)
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0
    detail = refused.json()["detail"]
    # Must read as a refusal, never as an empty answer (homelab-ij8.99), and
    # must tell a working agent it is not the one being turned away.
    assert detail.startswith("REFUSED")
    assert "a working key is still accepted" in detail


def test_a_working_key_from_a_blocked_address_is_still_served(db):
    """The one that matters: a shared LAN address must not become an outage."""
    client = _client(db)
    for _ in range(4):
        _bad(client)
    token, _ = keys.mint(db, agent="good-seat")
    response = client.get(
        "/api/v1/artifacts",
        headers={"Authorization": f"Bearer {token}", "x-real-ip": "203.0.113.9"},
    )
    assert response.status_code == 200
    # ...and it does not clear the count, or a good seat would keep resetting a
    # broken one on the same address and it would never be told to stop.
    assert _bad(client).status_code == 429


def test_a_missing_key_counts_too(db):
    client = _client(db)
    for _ in range(3):
        last = client.get("/api/v1/artifacts", headers={"x-real-ip": "198.51.100.7"})
    assert last.status_code == 429


def test_another_address_is_unaffected(db):
    client = _client(db)
    for _ in range(4):
        _bad(client)
    assert _bad(client, address="198.51.100.7").status_code == 401


def test_the_log_names_the_address_and_never_the_key(db, auth_log):
    client = _client(db)
    secretish = "art_" + "Q" * 43
    for _ in range(5):
        client.get(
            "/api/v1/artifacts",
            headers={
                "Authorization": f"Bearer {secretish}",
                "x-real-ip": "203.0.113.9",
            },
        )
    lines = [r.getMessage() for r in auth_log.records if r.name == "ream.auth"]
    # Two plain failures, then the one that blocked -- and silence after it.
    assert len(lines) == 3, lines
    assert all("203.0.113.9" in line for line in lines)
    assert "refusing bad keys" in lines[-1]
    assert not any("QQQQ" in line or "art_" in line for line in lines)


def test_the_gallery_host_and_the_api_host_share_one_count(db, monkeypatch):
    """Switching names must not double a caller's allowance."""
    monkeypatch.setenv("REAM_API_HOST", API_HOST)
    client = TestClient(create_app(db))
    statuses = []
    for i in range(ratelimit.THRESHOLD):
        host = GALLERY_HOST if i % 2 else API_HOST
        statuses.append(
            client.get(
                "/api/v1/artifacts",
                headers={
                    "host": host,
                    "Authorization": "Bearer art_nope",
                    "x-real-ip": "203.0.113.9",
                },
            ).status_code
        )
    assert statuses[:-1] == [401] * (ratelimit.THRESHOLD - 1)
    assert statuses[-1] == 429
