"""The CSP advisory: what it catches, what it must not catch, and what it never says.

The value of this check is entirely in it being right about the policy the
content origin actually serves. A shape that fires on a construct the CSP
permits trains authors to ignore warnings; a shape that stays silent on one it
blocks is the failure this whole check exists to close. So each shape is
pinned here to the directive that justifies it, and the pins read
`content._BASE_CSP` itself rather than a copy of it.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import content, csp, keys
from artifacts.app import create_gallery_app

CLEAN = (
    "<!doctype html>\n"
    "<html><head><title>Board</title>\n"
    "<style>body { font-family: system-ui; background: url(data:image/gif;base64,R0lGOD) }</style>\n"
    "</head><body>\n"
    '<img src="data:image/png;base64,iVBOR" alt="">\n'
    '<script src="/app.js"></script>\n'
    "<script>const x = 1;</script>\n"
    "</body></html>\n"
)


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def shapes(body: str) -> list[str]:
    return [warning.shape for warning in csp.check(body)]


# ---------------------------------------------------------------- the shapes


@pytest.mark.parametrize(
    ("shape", "body"),
    [
        ("remote_script", '<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>'),
        ("remote_script", '<script defer src="//cdn.example.com/x.js"></script>'),
        (
            "remote_stylesheet",
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">',
        ),
        ("remote_css_url", "@font-face { src: url('https://fonts.gstatic.com/s/i.woff2') }"),
        ("css_import", '@import url("https://example.com/base.css");'),
        ("remote_image", '<img src="https://example.com/logo.png" alt="">'),
        ("media_element", "<video controls></video>"),
        ("media_element", '<audio src="data:audio/wav;base64,UklGR"></audio>'),
        ("iframe", '<iframe src="/other"></iframe>'),
        ("form_element", '<form method="post"><button>Go</button></form>'),
        ("base_element", '<base href="/">'),
        ("web_worker", 'const w = new Worker("/worker.js");'),
        ("remote_connect", 'fetch("https://api.example.com/data")'),
        ("remote_connect", 'xhr.open("GET", "https://api.example.com/data")'),
        ("window_claude", 'const db = await window.claude.use("db");'),
        ("window_claude", 'claude.use("artifact")'),
    ],
)
def test_a_blocked_construct_is_reported(shape, body):
    assert shape in shapes(body)


@pytest.mark.parametrize(
    "body",
    [
        CLEAN,
        '<script src="/app.js"></script>',
        '<script src="app.js"></script>',
        '<img src="data:image/png;base64,iVBOR">',
        '<link rel="icon" href="/favicon.ico">',
        "<style>body { background: url(data:image/gif;base64,R0lGOD) }</style>",
        "<style>body { background: url(/local.png) }</style>",
        "<p>Nothing here at all.</p>",
    ],
)
def test_what_the_policy_permits_is_never_reported(body):
    assert csp.check(body) == []


def test_a_clean_page_says_nothing():
    assert csp.check(CLEAN) == []


# --------------------------------------------------------- what it never says


def test_a_warning_never_carries_the_matched_text():
    """The scanner's discipline, and for the same reason: a page body can hold
    anything, including the one string that must not travel back in a response
    or into a log. Only the shape name, the line, and the shape's own fixed
    hint ever leave this module."""
    hostile_src = "https://user:hunter2fake@cdn.example.com/x.js"
    warnings = csp.check(f'<script src="{hostile_src}"></script>')
    assert warnings, "the fixture must actually fire a shape"
    for warning in warnings:
        rendered = f"{warning.shape} {warning.line} {warning.hint}"
        assert "hunter2fake" not in rendered
        assert "cdn.example.com" not in rendered


def test_the_hint_belongs_to_the_shape_not_the_body():
    one = csp.check('<script src="https://a.example.com/x.js"></script>')[0]
    two = csp.check('<script src="https://b.example.com/y.js"></script>')[0]
    assert one.hint == two.hint


# ------------------------------------------------------------------- the line


def test_the_line_is_the_line_in_the_body():
    body = "<!doctype html>\n<p>one</p>\n<form></form>\n<p>three</p>\n"
    (warning,) = csp.check(body)
    assert warning.shape == "form_element"
    assert warning.line == 3


def test_every_line_of_a_repeated_shape_is_reported_up_to_the_bound():
    body = "\n".join("<form></form>" for _ in range(3))
    assert [w.line for w in csp.check(body)] == [1, 2, 3]


# ------------------------------------------------------------------ the bounds


def test_one_shape_repeated_many_times_stays_bounded():
    body = "\n".join("<form></form>" for _ in range(500))
    found = csp.check(body)
    assert len(found) == csp._MAX_LINES_PER_SHAPE
    assert {w.shape for w in found} == {"form_element"}


def test_a_body_full_of_everything_stays_under_the_total_bound():
    one_of_each = (
        '<script src="https://a/x.js"></script>\n'
        '<link href="https://a/x.css">\n'
        "@import url(https://a/y.css);\n"
        '<img src="https://a/i.png">\n'
        "<video></video>\n"
        "<iframe></iframe>\n"
        "<form></form>\n"
        "<base>\n"
        'new Worker("/w.js")\n'
        'fetch("https://a/d")\n'
        "window.claude\n"
        "background: url(https://a/f.woff2)\n"
    )
    found = csp.check(one_of_each * 50)
    assert len(found) <= csp._MAX_WARNINGS_TOTAL


# ------------------------------------- the pins: a shape means a real directive


def test_every_shape_is_justified_by_the_policy_actually_served():
    """Each shape names the directive that makes it true. A directive listed
    under `present` must appear in the served CSP; one listed under `absent`
    must NOT, because the shape's whole argument is that it falls back to
    `default-src 'none'`. Change the policy and this fails, which is the
    point: the hints tell authors what the browser will do, and a hint that
    has quietly stopped being true is worse than no hint."""
    served = content._BASE_CSP
    present = {
        "remote_script": "script-src 'self' 'unsafe-inline'",
        "remote_stylesheet": "style-src 'self' 'unsafe-inline'",
        "remote_css_url": "font-src 'self'",
        "css_import": "style-src 'self' 'unsafe-inline'",
        "remote_image": "img-src 'self' data:",
        "form_element": "form-action 'none'",
        "base_element": "base-uri 'none'",
        "web_worker": "worker-src 'none'",
    }
    absent = {"media_element": "media-src", "iframe": "frame-src"}

    assert "default-src 'none'" in served, (
        "every 'absent' pin below argues from this fallback"
    )
    for shape, directive in present.items():
        assert directive in served, f"{shape} claims a directive the CSP no longer has"
    for shape, directive in absent.items():
        assert directive not in served, (
            f"{shape} says {directive} falls back to default-src, but it is now set"
        )

    pinned = set(present) | set(absent) | {"remote_connect", "window_claude"}
    declared = {shape for shape, _pattern, _hint in csp._SHAPES}
    assert declared == pinned, "a shape was added or removed without a pin"


def test_connect_src_is_never_wider_than_self():
    """remote_connect's hint says "'self' at most". Both spellings the content
    origin serves are checked, not just the default one."""
    for live in (True, False):
        header = content.headers_for(live)["content-security-policy"]
        assert "connect-src 'self';" in header or "connect-src 'none';" in header


# ------------------------------------------------------- through the publish API


def test_publishing_a_broken_page_succeeds_and_says_what_will_not_run(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    body = (
        "<!doctype html>\n"
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">\n'
        "<form></form>\n"
    )
    response = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": body}, headers=auth(token)
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["id"] and payload["revision"]
    found = {(w["shape"], w["line"]) for w in payload["warnings"]}
    assert found == {("remote_stylesheet", 2), ("form_element", 3)}
    assert all(w["hint"] for w in payload["warnings"])


def test_a_clean_publish_returns_an_empty_warnings_list(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": CLEAN}, headers=auth(token)
    )
    assert response.status_code == 201
    assert response.json()["warnings"] == []


def test_patching_warns_about_the_new_body_not_the_old_one(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": CLEAN}, headers=auth(token)
    ).json()
    assert first["warnings"] == []
    second = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={
            "body": CLEAN + "<iframe></iframe>\n",
            "expected_revision": first["revision"],
        },
        headers=auth(token),
    )
    assert second.status_code == 200
    assert [w["shape"] for w in second.json()["warnings"]] == ["iframe"]


def test_nothing_inside_a_comment_is_reported():
    """A commented-out construct never runs, so the CSP never blocks it.
    Reporting it is a false alarm on a correct page, which is how a warning
    becomes noise (homelab-ij8.50 found the same thing for decision slots)."""
    body = (
        "<!doctype html>\n"
        "<!-- once we self-host it:\n"
        '<script src="https://cdn.example/x.js"></script>\n'
        "-->\n"
        '<!-- --!><iframe src="https://example.com"></iframe>\n'
    )
    assert [(w.shape, w.line) for w in csp.check(body)] == [("iframe", 5)], (
        "the commented script is silent, the iframe after an incorrectly "
        "closed comment is not, and the multi-line comment kept its lines"
    )
