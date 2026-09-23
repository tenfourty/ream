"""`artifacts-publish --check`: refuse a page before it half-renders.

homelab-ij8.29, reported by the cove seat on 19 Sep 2026 as a first user: a
page that violates the CSP does not error, it silently loses its styling or its
images -- "the worst failure mode for something you publish and walk away
from". They hand-rolled a grep for `<form`, `src=`, `<link`, `@import` and any
non-gallery URL, and said every agent would rediscover it.

The point of doing it here is that it uses the SAME shape table the service
does (`artifacts.csp`), derived from the real directives in the served policy,
so it cannot drift from what the browser will do. A grep in each agent's own
repo certainly would.
"""

import shutil

import pytest

from artifacts import client

CLEAN = (
    "<!doctype html><html><head><title>Good</title>"
    "<style>p{color:#333}</style></head>"
    "<body><p>Fine.</p></body></html>"
)


def _write(tmp_path, body, name="page.html"):
    path = tmp_path / name
    path.write_text(body)
    return str(path)


def test_a_clean_page_passes_and_says_so(tmp_path, capsys):
    assert client.main(["--check", _write(tmp_path, CLEAN)]) == 0
    assert "nothing the serving policy refuses" in capsys.readouterr().out


def test_a_page_the_policy_would_break_is_REFUSED(tmp_path, capsys):
    """Refusing is the whole difference from the server's advisory warning.
    The server cannot refuse -- the author cannot see the page and so cannot
    see what upset it. Here the author is looking at the file."""
    body = (
        '<!doctype html><html><head><title>Bad</title>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
        "</head><body><p>x</p></body></html>"
    )
    assert client.main(["--check", _write(tmp_path, body)]) == 1
    err = capsys.readouterr().err
    assert "remote_stylesheet" in err
    assert "half-render" in err, "it says what will actually happen"


def test_it_names_the_file_and_the_line(tmp_path, capsys):
    """So it reads like every other compiler and lands in an editor."""
    body = (
        "<!doctype html><html><head><title>T</title></head><body>\n"
        '<form action="/x"></form>\n</body></html>'
    )
    path = _write(tmp_path, body)
    client.main(["--check", path])
    assert f"{path}:2: form_element" in capsys.readouterr().err


def test_it_needs_no_key_and_makes_no_request(tmp_path, capsys, monkeypatch):
    """Half the value is running it in CI, or before you have a key, or on a
    page nobody has decided to publish. Proved by pointing the key file at
    nothing and the API at an address that would fail loudly."""
    monkeypatch.setenv("ARTIFACTS_KEY_FILE", str(tmp_path / "no-such-key"))
    monkeypatch.setenv("ARTIFACTS_API", "https://127.0.0.1:1/api/v1")
    assert client.main(["--check", _write(tmp_path, CLEAN)]) == 0


def test_a_missing_file_is_a_usage_error_not_a_pass(tmp_path, capsys):
    """A checker that cannot read its input must not report success."""
    assert client.main(["--check", str(tmp_path / "nope.html")]) == 2
    assert "no such file" in capsys.readouterr().err


# --- the inline-script half, from the cove seat's own note --------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime here")
def test_a_script_that_does_not_parse_is_caught(tmp_path, capsys):
    """"it catches a trailing-comma typo for nothing, in a validator that
    already parses the file." A page whose script does not parse renders as a
    page with no script -- the same silent half-failure, by another route."""
    body = (
        "<!doctype html><html><head><title>T</title></head><body>\n"
        "<script>const a = {b: 1,,};</script>\n</body></html>"
    )
    assert client.main(["--check", _write(tmp_path, body)]) == 1
    err = capsys.readouterr().err
    assert "inline script at line 2" in err
    assert "SyntaxError" in err


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime here")
def test_a_script_with_a_src_is_left_to_the_csp_shapes(tmp_path, capsys):
    """It is somebody else's file, it is already reported as remote_script,
    and there is nothing local to parse."""
    body = (
        '<!doctype html><html><head><title>T</title>'
        '<script src="https://cdn.example/x.js"></script>'
        "</head><body><p>x</p></body></html>"
    )
    client.main(["--check", _write(tmp_path, body)])
    err = capsys.readouterr().err
    assert "remote_script" in err
    assert "does not parse" not in err


def test_a_missing_node_is_reported_rather_than_passing_quietly(
    tmp_path, capsys, monkeypatch
):
    """A clean result that silently skipped half the checks is the shape this
    repo keeps meeting: a green signal that does not mean what it reads as."""
    monkeypatch.setattr(client.shutil, "which", lambda _name: None)
    assert client.main(["--check", _write(tmp_path, CLEAN)]) == 0
    assert "node is not installed here" in capsys.readouterr().err


def test_it_uses_the_services_own_shape_table_not_a_copy(tmp_path):
    """The reason this belongs here rather than in each agent's repo. If it
    ever grows its own patterns they will drift from the policy actually
    served, which is the failure the cove seat's hand-rolled grep would have
    had."""
    from artifacts import csp

    findings, _syntax, _node = client.check_page(
        '<html><body><iframe src="https://x.example"></iframe></body></html>'
    )
    assert [f.shape for f in findings] == [
        f.shape for f in csp.check(
            '<html><body><iframe src="https://x.example"></iframe></body></html>'
        )
    ]


# --- the traps a directive does not cover (homelab-ij8.36) --------------------


def test_a_placeholder_title_is_refused_here_too(tmp_path, capsys):
    """The page renders perfectly and is still wrong: the first board
    published here read PAGE TITLE in the browser tab for two hours, because
    an agent cannot look at its own page."""
    body = '<!doctype html>\n<html><head><title>PAGE TITLE</title></head>\n<body><p>x</p></body></html>\n'
    assert client.main(["--check", _write(tmp_path, body)]) == 1
    err = capsys.readouterr().err
    assert "placeholder_title" in err, "the specific trap is named"
    assert ":2:" in err, "on its own line"


def test_a_gallery_link_that_would_go_nowhere_is_refused(tmp_path, capsys):
    body = (
        '<!doctype html>\n<html><head><title>Board</title></head>\n'
        '<body><a href="https://artifacts.tenfourty.site/a/abc">Wall</a></body></html>\n'
    )
    assert client.main(["--check", _write(tmp_path, body)]) == 1
    assert "gallery_link_same_tab" in capsys.readouterr().err


def test_the_two_kinds_of_problem_are_counted_together(tmp_path, capsys):
    """One question -- what is wrong with this file? -- so one list and one
    count. Which module noticed is not the reader's business."""
    body = (
        '<!doctype html>\n<html><head><title>PAGE TITLE</title>\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">\n'
        '</head><body><p>x</p></body></html>\n'
    )
    assert client.main(["--check", _write(tmp_path, body)]) == 1
    err = capsys.readouterr().err
    assert "2 problems" in err
    assert "placeholder_title" in err and "remote_stylesheet" in err


def test_check_runs_the_services_own_secret_scan(tmp_path, capsys):
    """homelab-ij8.106: the first outside agent's page passed --check and was
    then refused on publish (422, password_assignment). --check ran only the CSP
    half; it now runs the same scan the service refuses with."""
    page = tmp_path / "page.html"
    page.write_text(
        "<!doctype html><html><head><title>Setup</title></head>"
        "<body><pre>DB_PASSWORD=hunter2fake</pre></body></html>"
    )
    assert client.main(["--check", str(page)]) == 1
    err = capsys.readouterr().err
    assert "password_assignment" in err and "REFUSES" in err
    assert "hunter2fake" not in err, "say which line and what shape, never echo it"


def test_a_clean_page_says_the_scan_ran(tmp_path, capsys):
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><html><head><title>t</title></head><body><p>hi</p></body></html>")
    assert client.main(["--check", str(page)]) == 0
    assert "nothing shaped like a secret" in capsys.readouterr().out
