"""The rest of the settings (core-and-plugins design 3.1, 3.3; slice 2).

The suite runs as our deployment does (tests/conftest.py sets the origins and the proxy
header as the manifest does). A test of a fresh install clears every setting first.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from ream import client, hosts, mail, ratelimit, settings, store
from ream.app import create_app
from ream.serve import app_from_env

PAGE = "<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>"


@pytest.fixture()
def fresh(monkeypatch):
    """A fresh install: none of the settings' variables set."""
    for name in settings._ENV.values():
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("ARTIFACTS_" + name.removeprefix("REAM_"), raising=False)


def test_the_suite_runs_with_the_manifests_origins_and_header():
    s = settings.current()
    assert s.gallery_origin == "https://artifacts.tenfourty.site"
    assert s.content_domain == "tenfourty.com"
    assert s.trusted_ip_header == "X-Real-IP"
    assert store.url_for("a" * 26) == f"https://{'a' * 26}.tenfourty.com/"


def test_every_variable_is_one_field_and_back():
    assert len(set(settings._ENV.values())) == len(settings._ENV)
    for env in settings._ENV.values():
        assert env.startswith("REAM_")


def test_override_takes_any_setting_and_lets_go(monkeypatch):
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://one.example")
    with settings.override(gallery_origin="https://two.example"):
        assert store.gallery_origin() == "https://two.example"
    assert store.gallery_origin() == "https://one.example"


def test_the_repr_prints_no_secret(monkeypatch):
    monkeypatch.setenv("REAM_MAILGUN_KEY", "key-do-not-print")
    monkeypatch.setenv("REAM_NUDGE_TOKEN", "token-do-not-print")
    text = f"{settings.current()!r} {settings.current()}"
    assert "do-not-print" not in text


# --- a fresh install ---------------------------------------------------------------------


def test_a_fresh_install_serves_from_loopback(fresh):
    s = settings.current()
    assert s.gallery_origin == "http://localhost:8000"
    assert s.content_domain == "localhost:8000"
    assert s.trusted_ip_header == ""
    assert not mail.enabled()
    assert settings.defaulted() == [
        "REAM_OWNER_ID",
        "REAM_OWNER_NAME",
        "REAM_OWNER_PRONOUN",
        "REAM_GALLERY_ORIGIN",
        "REAM_CONTENT_DOMAIN",
    ]
    assert settings.validate_for_serve().gallery_origin == "http://localhost:8000"


def test_a_fresh_install_puts_page_content_on_a_sibling_origin(fresh):
    ident = "a" * 26
    assert store.url_for(ident) == f"http://{ident}.localhost:8000/"
    assert store.page_url_for(ident) == f"http://localhost:8000/a/{ident}"
    host = hosts.normalise(f"{ident}.localhost:8000")
    assert hosts.artifact_id(host) == ident
    assert hosts.is_gallery(hosts.normalise("localhost:8000"))


def test_a_fresh_install_routes_a_page_origin_to_the_content_app(fresh, db):
    page = store.publish(db, "seat", "Plan", PAGE)
    app = create_app(db)
    reader = TestClient(app, base_url=f"http://{page.id}.localhost:8000")
    response = reader.get("/", follow_redirects=False)
    # Opened as a page of its own, the page origin sends a reader to the gallery.
    assert response.status_code == 303
    assert response.headers["location"] == f"http://localhost:8000/a/{page.id}"
    gallery = TestClient(app, base_url="http://localhost:8000")
    assert gallery.get("/healthz").json() == {"status": "ok"}


def test_the_gallery_csp_names_the_content_origin_scheme(fresh):
    from ream import gallery

    csp = gallery.gallery_headers()["content-security-policy"]
    assert "frame-src http://*.localhost:8000;" in csp


def test_the_real_entrypoint_logs_what_a_fresh_install_defaulted(
    fresh, dsn, monkeypatch, caplog
):
    monkeypatch.setenv("DATABASE_URL", dsn)
    with caplog.at_level(logging.WARNING, logger="ream.serve"):
        app_from_env()
    assert "REAM_GALLERY_ORIGIN" in caplog.text
    assert "REAM_OWNER_ID" in caplog.text


def _serve_warnings(caplog):
    with caplog.at_level(logging.WARNING, logger="ream.serve"):
        app_from_env()
    return [
        r.getMessage() for r in caplog.records if "TRUSTED_IP_HEADER" in r.getMessage()
    ]


def test_start_up_warns_when_exposed_without_a_trusted_ip_header(
    fresh, dsn, monkeypatch, caplog
):
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("REAM_BIND_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://gallery.example")
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "pages.example")
    assert len(_serve_warnings(caplog)) == 1


def test_start_up_is_silent_about_the_ip_header_when_set_or_on_loopback(
    fresh, dsn, monkeypatch, caplog
):
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("REAM_BIND_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://gallery.example")
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "pages.example")
    monkeypatch.setenv("REAM_TRUSTED_IP_HEADER", "X-Real-IP")
    assert _serve_warnings(caplog) == []
    monkeypatch.delenv("REAM_TRUSTED_IP_HEADER")
    monkeypatch.setenv("REAM_BIND_HOST", "127.0.0.1")
    caplog.clear()
    assert _serve_warnings(caplog) == []


def test_our_deployment_defaults_nothing_it_reports():
    # Production sets the owner and both origins: no fresh-install line.
    assert settings.defaulted() == []


# --- start-up refusals -------------------------------------------------------------------


def test_a_non_loopback_deployment_without_a_gallery_origin_refuses(fresh, monkeypatch):
    monkeypatch.setenv("REAM_BIND_HOST", "0.0.0.0")  # noqa: S104
    with pytest.raises(SystemExit) as exc:
        settings.validate_for_serve()
    assert "REAM_GALLERY_ORIGIN" in str(exc.value)
    assert "REAM_BIND_HOST" in str(exc.value)


def test_a_non_loopback_deployment_with_its_origins_starts(fresh, monkeypatch):
    monkeypatch.setenv("REAM_BIND_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://gallery.example")
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "pages.example")
    assert settings.validate_for_serve().content_domain == "pages.example"


def test_a_gallery_off_localhost_needs_a_content_domain(fresh, monkeypatch):
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://gallery.example")
    with pytest.raises(SystemExit) as exc:
        settings.validate_for_serve()
    assert "REAM_CONTENT_DOMAIN" in str(exc.value)


def test_validate_collects_every_problem_and_prints_no_value(monkeypatch):
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "gallery.example/path")
    monkeypatch.setenv("REAM_TRUSTED_IP_HEADER", "not a header")
    monkeypatch.setenv("REAM_MAILGUN_KEY", "key-do-not-print")
    monkeypatch.setenv("REAM_PARTNER_NAME", "p")
    monkeypatch.setenv("REAM_PARTNER_INBOUND_HOST", "h.example")
    with pytest.raises(SystemExit) as exc:
        settings.validate_for_serve()
    message = str(exc.value)
    for name in (
        "REAM_GALLERY_ORIGIN",
        "REAM_TRUSTED_IP_HEADER",
        "REAM_MAIL_FROM",
        "REAM_PARTNER_EVENTS_URL",
        "REAM_PARTNER_INBOUND_KEYS_FILE",
    ):
        assert name in message
    assert "do-not-print" not in message
    assert "gallery.example/path" not in message


def test_the_real_entrypoint_reports_settings_before_opening_the_database(
    dsn, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("REAM_PARTNER_NAME", "p")
    with pytest.raises(SystemExit) as exc:
        app_from_env()
    assert "half-configured" in str(exc.value)
    assert "REAM_PARTNER_EVENTS_URL" in str(exc.value)


# --- the pieces that lost a house default -------------------------------------------------


def test_mail_is_off_until_key_api_domain_and_sender_are_all_set(fresh, monkeypatch):
    monkeypatch.setenv("REAM_MAILGUN_KEY", "k")
    assert not mail.enabled()
    with pytest.raises(mail.MailFailed):
        mail.send("a@example.com", "s", "t")
    monkeypatch.setenv("REAM_MAILGUN_API", "https://api.mailgun.net")
    monkeypatch.setenv("REAM_MAILGUN_DOMAIN", "mg.example")
    monkeypatch.setenv("REAM_MAIL_FROM", "Pages <pages@mg.example>")
    assert mail.enabled()


def test_the_trusted_ip_header_is_a_setting(monkeypatch):
    headers = {"x-real-ip": "203.0.113.9", "cf-connecting-ip": "198.51.100.4"}
    assert ratelimit.client_address(headers, "10.0.0.1") == "203.0.113.9"
    with settings.override(trusted_ip_header="CF-Connecting-IP"):
        assert ratelimit.client_address(headers, "10.0.0.1") == "198.51.100.4"
    monkeypatch.delenv("REAM_TRUSTED_IP_HEADER")
    assert ratelimit.client_address(headers, "10.0.0.1") == "10.0.0.1"


def test_the_cli_endpoint_keeps_the_house_default_until_the_plugin_has_it(monkeypatch):
    monkeypatch.delenv("REAM_API", raising=False)
    assert client._api_base() == "https://artifacts.tenfourty.site/api/v1"
    monkeypatch.setenv("REAM_API", "http://127.0.0.1:8000/api/v1")
    assert client._api_base() == "http://127.0.0.1:8000/api/v1"


@pytest.mark.parametrize(
    ("values", "problem"),
    [
        ({"mcp_public_url": "https://artifacts.tenfourty.site"}, None),
        ({"mcp_public_url": "https://ARTIFACTS.tenfourty.site:443"}, None),
        (
            {
                "mcp_public_url": "https://api.tenfourty.site",
                "api_host": "api.tenfourty.site",
            },
            None,
        ),
        ({"mcp_public_url": "https://artifacts.tenfourty.site/mcp"}, "an origin"),
        ({"mcp_public_url": "https://artifacts.tenfourty.site/"}, "an origin"),
        ({"mcp_public_url": "artifacts.tenfourty.site"}, "an origin"),
        ({"mcp_public_url": "http://artifacts.tenfourty.site"}, "must be https"),
        ({"mcp_public_url": "https://elsewhere.example"}, "gallery origin"),
        ({"mcp_accept_keys": "1"}, None),
        ({"mcp_accept_keys": "maybe"}, "REAM_MCP_ACCEPT_KEYS must be 1 or 0"),
        ({"mcp_accept_keys": "0"}, "needs sign-in"),
        ({"mcp_sign_in": "maybe"}, "REAM_MCP_SIGN_IN must be on or off"),
        ({"mcp_sign_in": "off"}, None),
        # Until the sign-in itself is built, on refuses to start (tests override it).
        (
            {"mcp_public_url": "https://artifacts.tenfourty.site", "mcp_sign_in": "on"},
            "not finished in this version",
        ),
        (
            {
                "mcp_public_url": "https://artifacts.tenfourty.site",
                "mcp_sign_in": "on",
                "mcp_accept_keys": "0",
            },
            "not finished in this version",
        ),
        ({"mcp_trusted_client_hosts": "claude.ai, Zed.dev"}, None),
        ({"mcp_trusted_client_hosts": "none"}, None),
        ({"mcp_allowed_client_hosts": "https://claude.ai"}, "host names"),
        ({"mcp_allowed_client_hosts": "claude.ai/x"}, "host names"),
        ({"mcp_allowed_client_hosts": "none, claude.ai"}, "host names"),
        ({"mcp_self_registration_redirects": "localhost:3000"}, "host names"),
        ({"mcp_self_registration_redirects": "none"}, None),
    ],
)
def test_the_mcp_settings_are_checked_at_start(values, problem):
    with settings.override(**values) as current:
        found = [p for p in current.problems() if "MCP" in p]
    if problem is None:
        assert found == []
    else:
        assert len(found) == 1 and problem in found[0], found
        assert "elsewhere.example" not in found[0]


def test_sign_in_without_the_public_url_names_it():
    with settings.override(mcp_sign_in="on") as current:
        found = current.problems()
        assert "REAM_MCP_SIGN_IN=on needs REAM_MCP_PUBLIC_URL" in found
        assert current.mcp_signs_in() is False


def test_the_mcp_host_lists_read_as_sets():
    assert settings.host_list("") is None
    assert settings.host_list("none") == frozenset()
    assert settings.host_list(" Claude.ai , zed.dev. ") == {"claude.ai", "zed.dev"}
    assert settings.host_list("claude.ai:443") is settings.BAD_LIST
    with settings.override(mcp_allowed_client_hosts="none") as current:
        assert current.mcp_allowed_hosts() == frozenset()
    assert settings.current().mcp_allowed_hosts() is None
    assert settings.current().mcp_registration_hosts() == {"localhost"}
    with settings.override(mcp_self_registration_redirects="none") as current:
        assert current.mcp_registration_hosts() == frozenset()


def test_mcp_takes_keys_unless_told_not_to():
    # Spec section 7: without sign-in, keys are the only credential, so always taken
    # (and REAM_MCP_ACCEPT_KEYS=0 refuses to start); with sign-in, only when set to 1.
    assert settings.current().mcp_accepts_keys() is True
    with settings.override(mcp_accept_keys="0") as current:
        assert current.mcp_accepts_keys() is True
        assert any(
            "REAM_MCP_ACCEPT_KEYS=0 needs sign-in" in p for p in current.problems()
        )
    on = {"mcp_public_url": "https://artifacts.example", "mcp_sign_in": "on"}
    with settings.override(**on) as current:
        assert current.mcp_accepts_keys() is False
    with settings.override(**on, mcp_accept_keys="1") as current:
        assert current.mcp_accepts_keys() is True
    with settings.override(**on, mcp_accept_keys="0") as current:
        assert current.mcp_accepts_keys() is False
