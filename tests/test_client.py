"""`artifacts-publish`: a stdlib-only console script driven against the REAL app
over REAL HTTP (a uvicorn thread on an ephemeral loopback port), not TestClient --
the point of this client is that it works with nothing but the standard library and
a socket, so it is tested the way it will actually run. The loopback host must
answer as the gallery: `ARTIFACTS_GALLERY_ORIGIN` is set to this server's own
`http://127.0.0.1:<port>` so `hosts.is_gallery` (host, port stripped) matches the
`Host` header the client's own requests carry -- no test-only hook, just the same
environment-driven read `create_app`'s router already uses in `test_router.py`.
"""

import http.server
import json
import re
import threading
import time
import urllib.request

import pytest
import uvicorn

from artifacts import client, keys
from artifacts.app import create_app

SECRET_BODY = "<!doctype html><body>AKIA" + "IOSFODNN7EXAMPLE</body>"


class _LiveServer:
    def __init__(self, app):
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        while not self.server.started:
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture()
def live(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    server = _LiveServer(create_app(db))
    origin = server.start()
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", origin)
    monkeypatch.setenv("ARTIFACTS_API", f"{origin}/api/v1")
    try:
        yield origin
    finally:
        server.stop()


@pytest.fixture()
def key_file(db, tmp_path, monkeypatch):
    monkeypatch.delenv("ARTIFACTS_KEY_FILE", raising=False)
    token, _ = keys.mint(db, agent="ops-seat")
    path = tmp_path / "key"
    path.write_text(token)
    path.chmod(0o600)
    return path, token


def test_publish_then_update_then_list_round_trip(live, key_file, tmp_path, capsys):
    path, _token = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")

    rc = client.main(["--key-file", str(path), str(page), "--title", "Board"])
    assert rc == 0
    published = json.loads(capsys.readouterr().out.strip())
    assert published["url"].startswith("https://")
    artifact_id = published["id"]

    page.write_text("<!doctype html><body>hello</body></html>")
    rc = client.main(["--key-file", str(path), str(page), "--update", artifact_id])
    assert rc == 0
    updated = json.loads(capsys.readouterr().out.strip())
    assert updated["id"] == artifact_id
    assert updated["revision"] != published["revision"]

    rc = client.main(["--key-file", str(path), "--list"])
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert any(artifact_id in line and "Board" in line for line in lines)


def test_a_raced_update_is_refused_rather_than_overwriting_the_other_writer(
    live, key_file, tmp_path, capsys
):
    """THIS TEST USED TO ASSERT THE BUG.

    It was called "retries once on a stale revision then succeeds", and that is
    exactly what was wrong: the client caught the 409, re-read only the
    REVISION, and retried with the same body -- so whoever patched second
    silently destroyed the version that landed in between, and the losing agent
    was never told. The server's optimistic concurrency was correct; the client
    defeated it (homelab-ij8.54, found 19 Sep, fixed 21 Sep).

    A blind retry cannot be right: the body being sent was composed against a
    page that no longer exists. Merging is not the CLI's job -- it has no idea
    what the other writer meant. So it refuses and says how to recover, which
    is cheap now that `--fetch` exists.
    """
    path, token = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    rc = client.main(["--key-file", str(path), str(page), "--title", "Board"])
    assert rc == 0
    published = json.loads(capsys.readouterr().out.strip())

    # A raced write from elsewhere: the API directly, using the same key. `live` is
    # this test's own loopback server -- not a scheme or host from outside data.
    req = urllib.request.Request(  # noqa: S310
        f"{live}/api/v1/artifacts/{published['id']}",
        data=json.dumps(
            {
                "body": "<!doctype html><body>raced</body></html>",
                "expected_revision": published["revision"],
            }
        ).encode(),
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        raced = json.loads(resp.read())
    assert raced["revision"] != published["revision"]

    # This caller still thinks `published["revision"]` is current. It must be
    # refused, and the raced writer's version must survive untouched.
    page.write_text("<!doctype html><body>mine</body></html>")
    with pytest.raises(client.Conflict) as refused:
        client._patch(str(page), published["id"], published["revision"], token)

    said = str(refused.value)
    assert "was NOT" in said and "nothing was overwritten" in said
    assert "--fetch" in said, "it says how to recover"
    assert published["id"] in said

    # The other writer's page is still theirs.
    current = client._fetch(published["id"], token)
    assert "raced" in current
    assert "mine" not in current


def test_a_refused_secret_shaped_body_shows_the_422_detail_and_exits_1(
    live, key_file, tmp_path, capsys
):
    path, _token = key_file
    page = tmp_path / "page.html"
    page.write_text(SECRET_BODY)

    rc = client.main(["--key-file", str(path), str(page), "--title", "Leaky"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "422" in captured.err
    assert "aws_access_key_id" in captured.err
    assert "AKIA" not in captured.err


def test_a_group_readable_key_file_is_refused(live, tmp_path, capsys):
    path = tmp_path / "key"
    path.write_text("art_should-never-be-used")
    path.chmod(0o640)
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")

    rc = client.main(["--key-file", str(path), str(page), "--title", "Board"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "chmod 600" in captured.err


def test_the_key_never_appears_in_any_captured_output(live, key_file, tmp_path, capsys):
    path, token = key_file
    page = tmp_path / "page.html"
    page.write_text(SECRET_BODY)  # forces a 422 -- an error path too

    client.main(["--key-file", str(path), str(page), "--title", "Leaky"])
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err

    good = tmp_path / "good.html"
    good.write_text("<!doctype html><body>hi</body></html>")
    client.main(["--key-file", str(path), str(good), "--title", "Board"])
    client.main(["--key-file", str(path), "--list"])
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs) -> None:  # silence per-request access logs
        pass


def _recording_target(received: list) -> type:
    """A handler that records every request's headers and answers 200 JSON --
    used as the thing a refused redirect must never reach."""

    class Handler(_QuietHandler):
        def _record(self) -> None:
            received.append(dict(self.headers))
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _record
        do_POST = _record

    return Handler


def _redirector(status: int, location: str) -> type:
    class Handler(_QuietHandler):
        def _redirect(self) -> None:
            self.send_response(status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _redirect
        do_POST = _redirect

    return Handler


class _BareServer:
    """A plain http.server.HTTPServer in its own thread -- used for the redirect
    tests, which need to control the response themselves (status, Location), not
    a full FastAPI app."""

    def __init__(self, host: str, handler_cls: type):
        self.httpd = http.server.HTTPServer((host, 0), handler_cls)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        return f"http://{self.httpd.server_address[0]}:{self.httpd.server_address[1]}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)


@pytest.mark.parametrize("status", [302, 307])
def test_a_redirect_to_another_host_is_refused_and_never_reaches_it(
    tmp_path, monkeypatch, status
):
    """Even a same-host, allowed (loopback) API can answer with a redirect to a
    DIFFERENT host -- an Authelia bounce, an ingress misroute, anything. The
    client must never follow it: the target here must see NOTHING at all, not
    merely "a request with no Authorization"."""
    received: list = []
    target = _BareServer("127.0.0.2", _recording_target(received))
    target_origin = target.start()
    redirector = _BareServer(
        "127.0.0.1", _redirector(status, f"{target_origin}/elsewhere")
    )
    redirector_origin = redirector.start()
    try:
        monkeypatch.setenv("ARTIFACTS_API", f"{redirector_origin}/api/v1")
        path = tmp_path / "key"
        path.write_text("art_" + "a" * 40)
        path.chmod(0o600)

        rc = client.main(["--key-file", str(path), "--list"])

        assert rc == 1
        assert received == []
    finally:
        target.stop()
        redirector.stop()


def test_a_publish_redirected_302_also_never_reaches_the_target(tmp_path, monkeypatch):
    received: list = []
    target = _BareServer("127.0.0.2", _recording_target(received))
    target_origin = target.start()
    redirector = _BareServer(
        "127.0.0.1", _redirector(302, f"{target_origin}/elsewhere")
    )
    redirector_origin = redirector.start()
    try:
        monkeypatch.setenv("ARTIFACTS_API", f"{redirector_origin}/api/v1")
        path = tmp_path / "key"
        path.write_text("art_" + "a" * 40)
        path.chmod(0o600)
        page = tmp_path / "page.html"
        page.write_text("<!doctype html><body>hi</body></html>")

        rc = client.main(["--key-file", str(path), str(page), "--title", "Board"])

        assert rc == 1
        assert received == []
    finally:
        target.stop()
        redirector.stop()


def test_an_http_api_to_a_non_loopback_host_is_refused_before_any_connection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ARTIFACTS_API", "http://example.com/api/v1")
    called = []
    monkeypatch.setattr(
        client,
        "_opener",
        type("Boom", (), {"open": staticmethod(lambda *a, **k: called.append(1))})(),
    )
    path = tmp_path / "key"
    path.write_text("art_" + "a" * 40)
    path.chmod(0o600)

    rc = client.main(["--key-file", str(path), "--list"])

    assert rc == 2
    assert called == []


@pytest.mark.parametrize("host", ["127.0.0.1", "127.5.6.7", "::1", "localhost"])
def test_an_http_api_to_a_loopback_host_is_allowed(host, tmp_path, monkeypatch):
    """The scheme/host guard must not break the tests' own loopback servers --
    only a non-loopback http:// target is refused."""
    monkeypatch.setattr(client, "_check_url_scheme", client._check_url_scheme)
    # A bracketed literal for the IPv6 case, exactly as a URL requires.
    netloc = f"[{host}]" if ":" in host else host
    client._check_url_scheme(f"http://{netloc}/api/v1/artifacts")  # must not raise


@pytest.mark.parametrize(
    "bad_content",
    [
        "art_abcdefghijklmnopqrstuvwx\nEXTRA",  # an internal newline
        "art_abcdefghijklmnopqrst uvwx",  # an internal space
        "art_abcdefghijklmnopqrstuvwx\r\n",  # a trailing CRLF, not a bare LF
    ],
    ids=["internal-newline", "internal-space", "trailing-crlf"],
)
def test_a_malformed_key_file_is_refused_with_no_traceback(
    tmp_path, bad_content, capsys
):
    path = tmp_path / "key"
    path.write_bytes(bad_content.encode())
    path.chmod(0o600)

    rc = client.main(["--key-file", str(path), "--list"])

    assert rc == 2
    captured = capsys.readouterr()
    assert "abcdefghijklmnopqrstuvwx" not in captured.out
    assert "abcdefghijklmnopqrstuvwx" not in captured.err
    assert "Traceback" not in captured.err


def test_an_unexpected_exception_prints_only_its_class_name(
    live, key_file, monkeypatch, capsys
):
    path, token = key_file

    def _boom(*_args, **_kwargs):
        raise RuntimeError(f"leaking the key would say {token} here")

    monkeypatch.setattr(client, "_list", _boom)

    rc = client.main(["--key-file", str(path), "--list"])

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "error: RuntimeError"
    assert token not in captured.out
    assert token not in captured.err


def test_a_missing_file_is_a_usage_error_not_a_traceback(tmp_path, capsys):
    path = tmp_path / "key"
    path.write_text("art_" + "a" * 40)
    path.chmod(0o600)

    rc = client.main(
        ["--key-file", str(path), str(tmp_path / "does-not-exist.html"), "--title", "X"]
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "no such file" in captured.err


def test_a_non_json_2xx_response_is_treated_as_a_failure(tmp_path, monkeypatch, capsys):
    class Handler(_QuietHandler):
        def do_GET(self) -> None:
            body = b"not json at all"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = _BareServer("127.0.0.1", Handler)
    origin = server.start()
    try:
        monkeypatch.setenv("ARTIFACTS_API", f"{origin}/api/v1")
        path = tmp_path / "key"
        path.write_text("art_" + "a" * 40)
        path.chmod(0o600)

        rc = client.main(["--key-file", str(path), "--list"])

        assert rc == 1
        assert "unexpected response" in capsys.readouterr().err
    finally:
        server.stop()


def test_publish_and_update_print_only_id_url_revision(
    live, key_file, tmp_path, capsys
):
    path, _token = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")

    client.main(["--key-file", str(path), str(page), "--title", "Board"])
    published = json.loads(capsys.readouterr().out.strip())
    assert set(published.keys()) == {"id", "url", "revision"}

    client.main(["--key-file", str(path), str(page), "--update", published["id"]])
    updated = json.loads(capsys.readouterr().out.strip())
    assert set(updated.keys()) == {"id", "url", "revision"}


def test_a_409_costs_exactly_one_patch_attempt_and_no_silent_second(
    monkeypatch, tmp_path
):
    """The old version of this asserted TWO attempts and called that giving up.
    Two attempts was the bug: the second one carried the same body and, when it
    happened to win, destroyed whatever had landed in between. One attempt, then
    a refusal the caller can act on."""
    patch_attempts = []

    def fake_request(method, _url, _key, payload):
        if method == "PATCH":
            patch_attempts.append(payload)
            raise client.HTTPFailure(
                409, "the artifact moved on; read it and patch again"
            )
        raise AssertionError(f"unexpected {method}: a refusal must not re-read")

    monkeypatch.setattr(client, "_request", fake_request)
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>x</body></html>")

    with pytest.raises(client.Conflict):
        client._patch(str(page), "some-id", "stale-revision", "key")

    assert len(patch_attempts) == 1, "no second write, blind or otherwise"


def test_key_absence_also_covers_401_and_a_double_409_and_a_usage_error(
    live, tmp_path, capsys
):
    # A syntactically valid but unregistered key -- passes _read_key's shape
    # check, fails the server's own verification: a real 401, not a local refusal.
    fake_key = "art_" + "z" * 40
    path = tmp_path / "key"
    path.write_text(fake_key)
    path.chmod(0o600)

    rc = client.main(["--key-file", str(path), "--list"])
    assert rc == 1
    captured = capsys.readouterr()
    assert fake_key not in captured.out
    assert fake_key not in captured.err

    # A double-409 by monkeypatching `_request` (as above), driven through `main`.
    def fake_request(method, _url, _key, _payload=None):
        if method == "PATCH":
            raise client.HTTPFailure(409, "stale")
        return {"revision": "whatever"}

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(client, "_request", fake_request)
        page = tmp_path / "page.html"
        page.write_text("<!doctype html><body>x</body></html>")
        rc = client.main(["--key-file", str(path), str(page), "--update", "some-id"])
        assert rc == 1
        captured = capsys.readouterr()
        assert fake_key not in captured.out
        assert fake_key not in captured.err
    finally:
        monkeypatch.undo()

    # A usage error (missing FILE).
    rc = client.main(
        ["--key-file", str(path), str(tmp_path / "nope.html"), "--title", "X"]
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert fake_key not in captured.out
    assert fake_key not in captured.err


def test_a_csp_warning_reaches_stderr_and_leaves_stdout_parseable(
    live, key_file, tmp_path, capsys
):
    """An author who cannot see the rendered page must still hear that half of
    it will not run. The advisory goes to stderr on purpose: stdout stays the
    one-line JSON a caller pipes into something else."""
    path, _token = key_file
    page = tmp_path / "page.html"
    page.write_text(
        "<!doctype html>\n"
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">\n'
        "<body>hi</body></html>\n"
    )

    rc = client.main(["--key-file", str(path), str(page), "--title", "Board"])

    assert rc == 0
    captured = capsys.readouterr()
    published = json.loads(captured.out.strip())
    assert published["id"] and "warnings" not in published
    assert "remote_stylesheet" in captured.err
    assert "line 2" in captured.err
    assert "fonts.googleapis.com" not in captured.err


def test_a_clean_publish_prints_no_warning_line(live, key_file, tmp_path, capsys):
    path, _token = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")

    rc = client.main(["--key-file", str(path), str(page), "--title", "Board"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "warning:" not in captured.err


_NGINX_502 = (
    b"<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body>\r\n"
    b"<center><h1>502 Bad Gateway</h1></center>\r\n<hr><center>nginx</center>\r\n"
    b"</body>\r\n</html>\r\n"
)


class _Opener:
    """Answers with a scripted sequence: each entry is either a status to raise
    as an HTTPError, or bytes to return as a successful body."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def open(self, request, timeout=None):
        import io

        self.calls += 1
        item = self.script.pop(0) if self.script else self.script
        if isinstance(item, int):
            raise urllib.error.HTTPError(
                request.full_url, item, "rolling", {}, io.BytesIO(_NGINX_502)
            )

        class _R:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return item

        return _R()


def test_a_deploy_in_the_middle_of_a_publish_is_waited_out_not_failed(monkeypatch):
    """The service redeploys on every push, and while the new copy takes over the
    front door answers 502 for about a second -- measured 19 Sep 2026, each one
    returned in thirty milliseconds. Without a retry, any publish landing in that
    second fails, and an agent has to be told to try again by someone who happens
    to know a deploy was running."""
    slept = []
    monkeypatch.setattr(client.time, "sleep", slept.append)
    opener = _Opener(502, 503, b'{"id": "abc"}')
    monkeypatch.setattr(client, "_opener", opener)

    result = client._request(
        "GET", "https://example.invalid/api/v1/artifacts", "k", None
    )

    assert result == {"id": "abc"}
    assert opener.calls == 3
    # It must actually WAIT: retrying instantly would spend every attempt inside
    # the same second the front door is unhappy in.
    assert len(slept) == 2
    assert all(delay > 0 for delay in slept)


def test_a_service_that_only_rolls_fails_naming_the_status_not_the_page(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda _s: None)
    monkeypatch.setattr(client, "_opener", _Opener(502, 502, 502, 502))

    with pytest.raises(client.HTTPFailure) as excinfo:
        client._request("GET", "https://example.invalid/api/v1/artifacts", "k", None)

    assert excinfo.value.status == 502
    # The body is nginx's, addressed to a browser, and never meant for a terminal
    # or a transcript. Say what it was; do not show it.
    assert "Bad Gateway" not in str(excinfo.value.detail)
    assert "nginx" not in str(excinfo.value.detail)
    assert "not JSON" in str(excinfo.value.detail)


def test_the_services_own_refusal_is_shown_and_never_retried(monkeypatch):
    """A 422 is the service telling the caller something true about its request.
    Retrying it would be pointless and would hide the message."""
    import io

    calls = []

    class _Refuses:
        def open(self, request, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(
                request.full_url,
                422,
                "no",
                {},
                io.BytesIO(b'{"detail": "that body looks like a secret"}'),
            )

    monkeypatch.setattr(client, "_opener", _Refuses())

    with pytest.raises(client.HTTPFailure) as excinfo:
        client._request("POST", "https://example.invalid/api/v1/artifacts", "k", {})

    assert calls == [1]
    assert excinfo.value.detail == "that body looks like a secret"


# --- housekeeping from the command line --------------------------------------
#
# Jeremy, 20 Sep 2026: "ideally agents can take all the same actions a human
# can." An agent's hands are this script, so an action that exists only over
# HTTP is an action agents do not really have. Driven here against the real app
# for the same reason everything else in this file is: it is stdlib and a
# socket, and that is the claim.


def _publish_one(path, tmp_path, capsys, title="Board") -> str:
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    client.main(["--key-file", str(path), str(page), "--title", title])
    return json.loads(capsys.readouterr().out.strip())["id"]


def test_rename_sends_no_body_and_needs_no_file(live, key_file, tmp_path, capsys):
    path, _ = key_file
    artifact_id = _publish_one(path, tmp_path, capsys)
    (tmp_path / "page.html").unlink()  # the file is gone; the rename must not care

    rc = client.main(
        ["--key-file", str(path), "--rename", artifact_id, "--title", "A better name"]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out.strip())["title"] == "A better name"


def test_rename_without_a_title_is_a_usage_error_not_a_blank_name(
    live, key_file, tmp_path, capsys
):
    path, _ = key_file
    artifact_id = _publish_one(path, tmp_path, capsys)
    assert client.main(["--key-file", str(path), "--rename", artifact_id]) == 2
    assert "--title is required" in capsys.readouterr().err


def test_archive_and_unarchive_round_trip(live, key_file, tmp_path, capsys):
    path, _ = key_file
    artifact_id = _publish_one(path, tmp_path, capsys)

    assert client.main(["--key-file", str(path), "--archive", artifact_id]) == 0
    assert json.loads(capsys.readouterr().out.strip())["archived_at"] is not None

    assert client.main(["--key-file", str(path), "--unarchive", artifact_id]) == 0
    assert json.loads(capsys.readouterr().out.strip())["archived_at"] is None


def test_pin_and_unpin_round_trip(live, key_file, tmp_path, capsys):
    path, _ = key_file
    artifact_id = _publish_one(path, tmp_path, capsys)

    assert client.main(["--key-file", str(path), "--pin", artifact_id]) == 0
    assert json.loads(capsys.readouterr().out.strip())["pinned_at"] is not None

    assert client.main(["--key-file", str(path), "--unpin", artifact_id]) == 0
    assert json.loads(capsys.readouterr().out.strip())["pinned_at"] is None


def test_the_listing_keeps_its_three_columns_and_adds_state_only_when_there_is_some(
    live, key_file, tmp_path, capsys
):
    """Every caller of --list splits on tab and takes the first three fields.
    A state column that were always present would change the shape for all of
    them in order to report the common case, which is "nothing to say"."""
    path, _ = key_file
    artifact_id = _publish_one(path, tmp_path, capsys)

    client.main(["--key-file", str(path), "--list"])
    plain = capsys.readouterr().out.strip()
    # The FIRST THREE fields are the contract: every existing caller splits on
    # tab and takes id, title, url. Columns may be added after them -- a date
    # was, on 21 Sep, because finding stale pages otherwise cost an API call
    # each -- and that is why this asserts a prefix rather than a column count,
    # which is what it used to do and what broke when the date arrived.
    assert plain.split("\t")[:3] == [artifact_id, "Board", plain.split("\t")[2]]
    assert plain.split("\t")[2].startswith("https://")
    assert re.match(r"\d{4}-\d{2}-\d{2}$", plain.split("\t")[3]), "then the date"

    client.main(["--key-file", str(path), "--pin", artifact_id])
    capsys.readouterr()
    client.main(["--key-file", str(path), "--list"])
    pinned = capsys.readouterr().out.strip()
    assert pinned.split("\t")[:3] == plain.split("\t")[:3], (
        "the first three are unmoved"
    )
    # Date then state: the date is always there, the state only when there is
    # one, so state is the LAST field rather than a fixed index.
    assert pinned.split("\t")[-1] == "pinned"
    assert re.match(r"\d{4}-\d{2}-\d{2}$", pinned.split("\t")[3])


def test_a_page_this_key_does_not_own_is_refused(live, key_file, db, tmp_path, capsys):
    """The scope is the service's, not this script's -- but an agent has to see
    that it failed, so the exit code and the message matter here."""
    from artifacts import store

    keys.mint(db, agent="another-seat")
    theirs = store.publish(
        db, "another-seat", "Not yours", "<!doctype html><body>x</body></html>"
    )
    path, _ = key_file
    assert client.main(["--key-file", str(path), "--archive", theirs.id]) == 1
    assert "404" in capsys.readouterr().err
    assert store.get_owned(db, "another-seat", theirs.id).archived_at is None


def test_duplicate_makes_a_new_page_and_prints_its_address(
    live, key_file, tmp_path, capsys
):
    """It prints like a publish, not like the other housekeeping flags: the
    others change a page and hand back the same one, this makes a NEW page and
    the caller's next move needs its id and url."""
    path, _ = key_file
    artifact_id = _publish_one(path, tmp_path, capsys, title="Original")

    assert client.main(["--key-file", str(path), "--duplicate", artifact_id]) == 0
    copy = json.loads(capsys.readouterr().out.strip())
    assert copy["id"] != artifact_id
    assert copy["url"].startswith("https://")

    client.main(["--key-file", str(path), "--list"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert any("Original (copy)" in line for line in lines)
    assert len(lines) == 2, "the original is still there beside it"
