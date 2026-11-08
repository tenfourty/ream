"""REAM_FORMER_HOST: the gallery's previous name after a move. It serves the agent API for good
(agents' clients refuse redirects, so an API path must never be forwarded) and forwards every
other path to the gallery origin with a 308. Unset, it does not exist."""

import pytest
from fastapi.testclient import TestClient

from ream import keys, ratelimit, settings
from ream.app import create_app
from ream.traps import check

GALLERY_HOST = "artifacts.tenfourty.site"
FORMER = "old.tenfourty.site"
PAGE = "<!doctype html><title>t</title><body>hi</body></html>"


@pytest.fixture()
def former(monkeypatch):
    monkeypatch.setenv("REAM_FORMER_HOST", FORMER)
    return FORMER


@pytest.fixture()
def client(db) -> TestClient:
    return TestClient(create_app(db), follow_redirects=False)


def auth(token):
    return {"host": FORMER, "Authorization": f"Bearer {token}"}


def test_unset_the_name_is_404_like_any_other(client, monkeypatch):
    monkeypatch.delenv("REAM_FORMER_HOST", raising=False)
    monkeypatch.delenv("ARTIFACTS_FORMER_HOST", raising=False)
    for path in ("/healthz", "/api/v1/me", "/"):
        response = client.get(path, headers={"host": FORMER})
        assert response.status_code == 404
        assert response.text == "not found"


def test_the_old_setting_name_is_read_too(client, monkeypatch):
    monkeypatch.delenv("REAM_FORMER_HOST", raising=False)
    monkeypatch.setenv("ARTIFACTS_FORMER_HOST", FORMER)
    assert client.get("/healthz", headers={"host": FORMER}).status_code == 200


def test_healthz_is_served(client, former):
    response = client.get("/healthz", headers={"host": former})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("word", ["pages", "artifacts"])
def test_both_api_words_are_served_for_a_key_get_and_post(client, former, db, word):
    token, _ = keys.mint(db, agent="old-address-seat")
    listing = client.get(f"/api/v1/{word}", headers=auth(token))
    assert listing.status_code == 200
    made = client.post(
        f"/api/v1/{word}", json={"title": "Via old", "body": PAGE}, headers=auth(token)
    )
    assert made.status_code == 201, "a POST must be served, never forwarded"
    assert "location" not in made.headers


def test_a_missing_key_on_the_old_name_is_refused_not_forwarded(client, former):
    response = client.get("/api/v1/pages", headers={"host": former})
    assert response.status_code == 401


def test_the_failed_key_allowance_is_shared_with_the_gallery_name(client, former):
    headers = {"Authorization": "Bearer nope-not-a-key"}
    codes = []
    for i in range(ratelimit.THRESHOLD + 2):
        host = FORMER if i % 2 else GALLERY_HOST
        codes.append(
            client.get("/api/v1/pages", headers={"host": host, **headers}).status_code
        )
    assert 429 in codes, "alternating names does not double the allowance"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/a/abc?x=1&y=2",
        "/gallery.js",
        "/agents.txt",
        "/connect",
        "/api/v2/me",
        "/api/v1",
        "/healthz/x",
    ],
)
def test_every_other_path_is_a_308_to_the_gallery_origin(client, former, path):
    response = client.get(path, headers={"host": former})
    assert response.status_code == 308
    location = response.headers["location"]
    assert location == f"https://{GALLERY_HOST}{path}"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "immutable" not in response.headers["cache-control"]


def test_a_form_post_is_forwarded_keeping_its_method(client, former):
    response = client.post("/d/xyz/answer", data={"a": "1"}, headers={"host": former})
    assert response.status_code == 308


def test_the_location_never_comes_from_the_host_header(client, former):
    response = client.get("/x", headers={"host": f"{FORMER}:8443"})
    assert response.status_code == 308
    assert response.headers["location"] == f"https://{GALLERY_HOST}/x"


def test_the_gallery_name_is_unchanged(client, former):
    assert client.get("/", headers={"host": GALLERY_HOST}).status_code == 200


def test_an_unknown_name_is_still_404(client, former):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 404


def test_the_former_name_cannot_shadow_the_gallery(client, monkeypatch):
    """Both set the same by mistake: the gallery wins (and start-up refuses it)."""
    monkeypatch.setenv("REAM_FORMER_HOST", GALLERY_HOST)
    assert client.get("/", headers={"host": GALLERY_HOST}).status_code == 200


def test_agents_txt_and_the_card_name_only_the_new_origin(client, former):
    for path in ("/api/v1/agents.txt", "/api/v1/skill.md", "/api/v1/agent.json"):
        body = client.get(path, headers={"host": former}).text
        assert GALLERY_HOST in body, path
        assert FORMER not in body, path
    assert (
        client.get("/.well-known/agent.json", headers={"host": former}).status_code
        == 308
    )


def test_a_link_to_either_host_needs_a_new_tab_while_the_former_is_set(former):
    for host in (GALLERY_HOST, FORMER):
        found = check(
            "<!doctype html><title>t</title><body>"
            f'<a href="https://{host}/a/abc">wall</a></body></html>'
        )
        assert "gallery_link_same_tab" in [w.shape for w in found], host


def test_without_a_former_host_its_links_are_not_a_gallery_link(monkeypatch):
    monkeypatch.delenv("REAM_FORMER_HOST", raising=False)
    found = check(
        "<!doctype html><title>t</title><body>"
        f'<a href="https://{FORMER}/a">w</a></body></html>'
    )
    assert "gallery_link_same_tab" not in [w.shape for w in found]


@pytest.mark.parametrize(
    "value, word",
    [
        ("https://old.example", "host name"),
        ("old.example/x", "host name"),
        ("old.example:8000", "host name"),
        (GALLERY_HOST, "differ"),
    ],
)
def test_start_up_refuses_a_bad_former_host_naming_the_variable_only(
    monkeypatch, value, word
):
    monkeypatch.setenv("REAM_FORMER_HOST", value)
    with pytest.raises(SystemExit) as exc:
        settings.validate_for_serve()
    message = str(exc.value)
    assert "REAM_FORMER_HOST" in message and word in message
    assert value not in message


def test_start_up_refuses_the_api_host_as_former_host(monkeypatch):
    monkeypatch.setenv("REAM_API_HOST", "agents.example")
    monkeypatch.setenv("REAM_FORMER_HOST", "agents.example")
    with pytest.raises(SystemExit):
        settings.validate_for_serve()


def test_a_good_former_host_starts(former):
    assert settings.validate_for_serve().former_host == FORMER


BAD_VALUE = "sk-live-0123456789abcdef-not-a-real-value"


@pytest.mark.parametrize("which", ["gallery", "api", "former"])
def test_a_refused_body_is_a_422_that_repeats_nothing_on_every_host(
    db, monkeypatch, which
):
    monkeypatch.setenv("REAM_API_HOST", "api.tenfourty.site")
    monkeypatch.setenv("REAM_FORMER_HOST", FORMER)
    host = {"gallery": GALLERY_HOST, "api": "api.tenfourty.site", "former": FORMER}[
        which
    ]
    client = TestClient(create_app(db), follow_redirects=False)
    token, _ = keys.mint(db, agent="validation-seat")
    headers = {"host": host, "Authorization": f"Bearer {token}"}
    bodies = [
        {"title": BAD_VALUE},  # `body` missing
        {"title": BAD_VALUE, "body": PAGE, "format": BAD_VALUE},  # bad value
        {"title": BAD_VALUE, "body": {"nested": BAD_VALUE}},  # wrong type
    ]
    for payload in bodies:
        response = client.post("/api/v1/pages", json=payload, headers=headers)
        assert response.status_code == 422, which
        assert BAD_VALUE not in response.text, which
        assert "input" not in response.text, which
        assert "input" not in str(response.json()), which


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/x",
        "/\\evil.example/x",
        "/%2f%2fevil.example/x",
        "/a%0d%0aSet-Cookie:%20x=1",
        "/x?q=a%0d%0ab&r=<>\"'`{}|^&s=%00%ff;",
    ],
)
def test_odd_paths_still_forward_to_the_gallery_origin_only(client, former, path):
    response = client.get(path, headers={"host": former})
    assert response.status_code == 308
    location = response.headers["location"]
    assert location.startswith(f"https://{GALLERY_HOST}/")
    assert "\r" not in location and "\n" not in location
