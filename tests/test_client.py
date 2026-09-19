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
    rc = client.main(
        ["--key-file", str(path), str(page), "--update", artifact_id]
    )
    assert rc == 0
    updated = json.loads(capsys.readouterr().out.strip())
    assert updated["id"] == artifact_id
    assert updated["revision"] != published["revision"]

    rc = client.main(["--key-file", str(path), "--list"])
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert any(artifact_id in line and "Board" in line for line in lines)


def test_update_retries_once_on_a_stale_revision_then_succeeds(
    live, key_file, tmp_path, capsys
):
    """A concurrent patch lands between a caller's read and its own patch attempt:
    the first PATCH gets 409, `_patch_with_retry` re-reads and retries once, and
    that retry succeeds against the now-current revision. `_update` (what `main`
    calls) always reads its own starting revision first, so the only way to hand
    it a deliberately STALE one -- and so force the 409 this test is about -- is
    to drive `_patch_with_retry` directly, the same helper `_update` delegates to
    after its own fresh read.
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
            {"body": "<!doctype html><body>raced</body></html>",
             "expected_revision": published["revision"]}
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

    # This caller still thinks `published["revision"]` is current -- the retry
    # helper must notice the 409, re-read, and land its own retry.
    page.write_text("<!doctype html><body>mine</body></html>")
    result = client._patch_with_retry(
        str(page), published["id"], published["revision"], token
    )
    assert result["id"] == published["id"]
    assert result["revision"] not in (published["revision"], raced["revision"])


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
def test_a_malformed_key_file_is_refused_with_no_traceback(tmp_path, bad_content, capsys):
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


def test_publish_and_update_print_only_id_url_revision(live, key_file, tmp_path, capsys):
    path, _token = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")

    client.main(["--key-file", str(path), str(page), "--title", "Board"])
    published = json.loads(capsys.readouterr().out.strip())
    assert set(published.keys()) == {"id", "url", "revision"}

    client.main(
        ["--key-file", str(path), str(page), "--update", published["id"]]
    )
    updated = json.loads(capsys.readouterr().out.strip())
    assert set(updated.keys()) == {"id", "url", "revision"}


def test_a_second_409_gives_up_after_exactly_two_patch_attempts(monkeypatch, tmp_path):
    patch_attempts = []

    def fake_request(method, _url, _key, payload):
        if method == "PATCH":
            patch_attempts.append(payload)
            raise client.HTTPFailure(409, "the artifact moved on; read it and patch again")
        if method == "GET":
            return {"revision": "still-not-what-you-think"}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(client, "_request", fake_request)
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>x</body></html>")

    with pytest.raises(client.HTTPFailure) as excinfo:
        client._patch_with_retry(str(page), "some-id", "stale-revision", "key")

    assert excinfo.value.status == 409
    assert len(patch_attempts) == 2


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
