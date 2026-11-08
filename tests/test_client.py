"""`artifacts-publish`: a stdlib-only console script driven against the REAL app
over REAL HTTP (a uvicorn thread on an ephemeral loopback port), not TestClient --
the point of this client is that it works with nothing but the standard library and
a socket, so it is tested the way it will actually run. The loopback host must
answer as the gallery: `REAM_GALLERY_ORIGIN` is set to this server's own
`http://127.0.0.1:<port>` so `hosts.is_gallery` (host, port stripped) matches the
`Host` header the client's own requests carry -- no test-only hook, just the same
environment-driven read `create_app`'s router already uses in `test_router.py`.
"""

import asyncio
import contextlib
import http.client
import http.server
import json
import re
import threading
import time
import urllib.request

import pytest
import uvicorn

from ream import client, keys
from ream.app import create_app

SECRET_BODY = "<!doctype html><body>AKIA" + "IOSFODNN7EXAMPLE</body>"


class _PromptServer(uvicorn.Server):
    """uvicorn, minus the two fixed 0.1 s sleeps that made every test using a
    live server pay ~0.2 s on the way out: `main_loop` polls `should_exit`
    every 0.1 s, and `shutdown` sleeps 0.1 s after asking connections to close
    before it even starts waiting for them. Here the main loop wakes the moment
    `wake()` is called, and shutdown waits for the connections by polling
    their real state every few milliseconds. Tests only; the service's own
    uvicorn is untouched."""

    _GRACE = 3.0
    _wake: asyncio.Event | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    cancelled = 0

    async def main_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        while not await self.on_tick(0):  # counter 0: refresh the Date header
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)

    def wake(self) -> None:
        self.should_exit = True
        loop, event = self._loop, self._wake
        if loop is not None and event is not None:
            with contextlib.suppress(RuntimeError):  # loop already closed
                loop.call_soon_threadsafe(event.set)

    async def shutdown(self, sockets=None) -> None:
        for server in self.servers:
            server.close()
        for connection in list(self.server_state.connections):
            connection.shutdown()
        # A request still in flight gets its answer, for up to `_GRACE` --
        # shorter than stop()'s join, so a stuck request is reported as
        # that, never as a server that merely failed to stop.
        deadline = time.monotonic() + self._GRACE
        while (
            self.server_state.connections or self.server_state.tasks
        ) and time.monotonic() < deadline:
            await asyncio.sleep(0.002)
        self.cancelled = len(self.server_state.tasks)
        for task in self.server_state.tasks:
            task.cancel()
        for server in self.servers:
            await server.wait_closed()
        await self.lifespan.shutdown()


class _LiveServer:
    def __init__(self, app):
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = _PromptServer(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        while not self.server.started:
            time.sleep(0.002)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.wake()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "the live server did not stop within 5 s"
        assert not self.server.cancelled, (
            f"{self.server.cancelled} request(s) were still running"
            f" {self.server._GRACE:.0f} s after shutdown and were cancelled"
        )


@pytest.fixture()
def live(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    server = _LiveServer(create_app(db))
    origin = server.start()
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", origin)
    monkeypatch.setenv("REAM_API", f"{origin}/api/v1")
    try:
        yield origin
    finally:
        server.stop()


@pytest.fixture()
def key_file(db, tmp_path, monkeypatch):
    monkeypatch.delenv("REAM_KEY_FILE", raising=False)
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
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

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
        monkeypatch.setenv("REAM_API", f"{redirector_origin}/api/v1")
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
        monkeypatch.setenv("REAM_API", f"{redirector_origin}/api/v1")
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
    monkeypatch.setenv("REAM_API", "http://example.com/api/v1")
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
        monkeypatch.setenv("REAM_API", f"{origin}/api/v1")
        path = tmp_path / "key"
        path.write_text("art_" + "a" * 40)
        path.chmod(0o600)

        rc = client.main(["--key-file", str(path), "--list"])

        assert rc == 1
        assert "unexpected response" in capsys.readouterr().err
    finally:
        server.stop()


def test_stdout_is_a_whitelist_and_the_set_is_deliberate(
    live, key_file, tmp_path, capsys
):
    """The point of this test is not the particular fields -- it is that stdout
    is a WHITELIST, so a field the server starts returning can never reach a
    caller's parsing unreviewed. The set grew on 21 Sep and it grew on purpose.

    `page_url` because `url` is the page's own origin and 303s to the gallery,
    so the one field that looked pasteable was the one that did not take a
    reader to the page (homelab-ij8.42). `title` and `bytes` because the
    publisher cannot see what it published: a substitution that silently did
    nothing looked identical to one that worked (homelab-ij8.43).
    """
    path, _token = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    expected = {"id", "page_url", "url", "revision", "title", "bytes"}

    client.main(["--key-file", str(path), str(page), "--title", "Board"])
    published = json.loads(capsys.readouterr().out.strip())
    assert set(published.keys()) == expected

    client.main(["--key-file", str(path), str(page), "--update", published["id"]])
    updated = json.loads(capsys.readouterr().out.strip())
    assert set(updated.keys()) == expected


def test_the_two_addresses_are_not_the_same_and_one_is_for_a_person(
    live, key_file, tmp_path, capsys
):
    """An agent nearly pasted the content origin into a report to another
    session. It 303s, so it is not wrong exactly -- it is the address that
    looks right and is not the one to share."""
    path, _token = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    client.main(["--key-file", str(path), str(page), "--title", "Board"])
    published = json.loads(capsys.readouterr().out.strip())

    assert published["page_url"] != published["url"]
    assert published["page_url"].endswith(f"/a/{published['id']}")
    assert published["id"] in published["url"], "the origin still names the page"


def test_the_echo_says_what_was_actually_stored(live, key_file, tmp_path, capsys):
    """The reported case: an agent replaced two timestamp placeholders with sed
    and realised that had the sed failed, the response would have been
    identical and the page would have shown the raw placeholder to Jeremy."""
    path, _token = key_file
    page = tmp_path / "page.html"
    body = "<!doctype html><body>a real page, not a placeholder</body></html>"
    page.write_text(body)

    client.main(["--key-file", str(path), str(page), "--title", "Board"])
    seen = capsys.readouterr()
    published = json.loads(seen.out.strip())
    assert published["title"] == "Board"
    assert published["bytes"] == len(body.encode())
    # And in words, on stderr, where a person reads it.
    assert 'stored "Board"' in seen.err
    assert f"{len(body.encode())} bytes" in seen.err


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


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("The read operation timed out"),
        http.client.RemoteDisconnected("Remote end closed connection"),
        http.client.IncompleteRead(b"partial"),
        ConnectionResetError("reset by peer"),
    ],
    ids=["read-timeout", "remote-disconnected", "incomplete-read", "reset"],
)
def test_a_connection_lost_mid_response_is_an_http_failure(monkeypatch, failure):
    """urllib wraps only a failure to connect in URLError: a read that times
    out, or a connection dropped after it was made, escapes as itself. The
    answer waiter retries an HTTPFailure and nothing else, so on 22 Oct 2026 a
    read timeout while baba came back from a reboot killed it with a traceback."""

    class _Drops:
        def open(self, request, timeout=None):
            raise failure

    monkeypatch.setattr(client, "_opener", _Drops())

    with pytest.raises(client.HTTPFailure) as excinfo:
        client._request("GET", "https://example.invalid/api/v1/events", "k", None)

    assert excinfo.value.status == 0


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
    # The GALLERY address, not the page's own origin: this column is read by a
    # person and the origin 303s (homelab-ij8.42). Asserted as "the address of
    # this page in the gallery" rather than as "starts with https", which was
    # only ever true because `url_for` hardcodes the scheme -- the gallery
    # origin is whatever the deployment sets, and http on a loopback in tests.
    assert plain.split("\t")[2].endswith(f"/a/{artifact_id}")
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
    from ream import store

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


# --- how old is the copy you are running -------------------------------------


def test_an_unknown_flag_says_how_old_this_build_is(capsys):
    """homelab-31, 21 Sep: `--fetch` was committed on the 20th and the installed
    copy predated it, so a flag that exists answered `unrecognized arguments`,
    which "reads exactly like 'this feature does not exist'". Exactly the
    ambiguity the API's catch-all 404 was written to end, one layer out."""
    with pytest.raises(SystemExit) as exit_:
        client.main(["--no-such-flag"])
    assert exit_.value.code == 2

    err = capsys.readouterr().err
    assert "unrecognized arguments" in err, "argparse's own wording is kept"
    assert "installed" in err and client.installed_stamp() in err
    assert "uv tool install" in err, "and the command that fixes it"


def test_an_ordinary_usage_error_is_left_alone(capsys):
    """The hint is worth reading only because it is rare. Put it on every usage
    error -- a missing value, a bad path -- and it becomes furniture, which is
    how it stops being read on the one day it is the answer."""
    with pytest.raises(SystemExit):
        client.main(["--update"])  # expects a value, got none
    err = capsys.readouterr().err
    assert "expected one argument" in err
    assert "uv tool install" not in err


def test_the_age_can_be_asked_for_before_it_is_a_surprise(capsys):
    with pytest.raises(SystemExit) as exit_:
        client.main(["--version"])
    assert exit_.value.code == 0
    assert client.installed_stamp() in capsys.readouterr().out


def test_the_stamp_is_a_real_timestamp_not_a_version_string():
    """`artifacts` is 0.1.0 and always will be, so a version string cannot tell a
    fresh install from a six-day-old one. A date can."""
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", client.installed_stamp())


def test_fetch_reports_the_pages_warnings_and_keeps_stdout_the_page(
    live, key_file, tmp_path, capsys
):
    """Read-back is where a seat finds out what is wrong with a page it cannot
    see. The service sends the warnings; the client used to drop them, so a
    fetch that printed nothing proved nothing."""
    path, _token = key_file
    body = '<!doctype html>\n<script src="https://cdn.example/x.js"></script>\n'
    page = tmp_path / "page.html"
    page.write_text(body)
    assert client.main(["--key-file", str(path), str(page), "--title", "Board"]) == 0
    published = json.loads(capsys.readouterr().out.strip())

    assert client.main(["--key-file", str(path), "--fetch", published["id"]]) == 0
    out, err = capsys.readouterr()
    assert "warning: remote_script at line 2" in err
    assert "warning" not in out, "stdout is the page and nothing else"
    assert body in out


def test_the_listing_names_the_session_that_published_each_page(
    live, key_file, tmp_path, capsys
):
    """A key is shared by every session on its machine, so a listing under one
    says nothing about who published what -- which on 22 Sep 2026 produced a
    wrong attribution, a page another seat nearly rewrote on Jeremy's
    instruction, and four answers of his left uncollected because a departed
    session and a live one were indistinguishable.

    The service has recorded the session on every version since 20 September.
    This is the printer finally saying it.
    """
    path, _ = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    client.main(
        [
            "--key-file",
            str(path),
            str(page),
            "--title",
            "Board",
            "--session",
            "mine/111",
        ]
    )
    artifact_id = json.loads(capsys.readouterr().out.strip())["id"]

    client.main(["--key-file", str(path), "--list", "--session", "mine/111"])
    ours = capsys.readouterr().out.strip().split("\t")
    assert ours[:3] == [artifact_id, "Board", ours[2]], "the first three are unmoved"
    assert re.match(r"\d{4}-\d{2}-\d{2}$", ours[3]), "then the date"
    assert ours[4] == "mine/111 (you)", (
        "the session, marked as this caller's own -- the whole question being "
        "answered is 'is this mine?'"
    )

    # The SAME page, listed by a different session behind the SAME key. It is
    # still listed -- Jeremy ruled that a shared key may read and edit
    # everything it can reach -- but it must no longer read as this caller's.
    client.main(["--key-file", str(path), "--list", "--session", "theirs/999"])
    theirs = capsys.readouterr().out.strip().split("\t")
    assert theirs[:3] == ours[:3], "the same page to both"
    assert theirs[4] == "mine/111", "named, and NOT marked as the reader's own"


def test_a_page_with_no_recorded_session_adds_no_column(
    live, key_file, tmp_path, capsys
):
    """Unknown is not the same fact as nobody, and an empty column would be a
    fifth field every caller has to learn about in order to report nothing."""
    path, _ = key_file
    artifact_id = _publish_one(path, tmp_path, capsys)
    client.main(["--key-file", str(path), "--list"])
    fields = capsys.readouterr().out.strip().split("\t")
    assert fields[0] == artifact_id
    assert len(fields) == 4, "id, title, address, date -- and nothing invented"


def _raises_with(status, headers, body):
    """An opener whose one answer is an HTTPError carrying REAL message headers
    (case-insensitive, as urllib hands them over), not a dict."""
    import email.message
    import io

    message = email.message.Message()
    for name, value in headers.items():
        message[name] = value

    class _O:
        def open(self, request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, status, "x", message, io.BytesIO(body)
            )

    return _O()


def test_a_cloudflare_challenge_is_named_as_one_not_blamed_on_the_service(monkeypatch):
    """23 Sep 2026: with the service behind Cloudflare's bot protection, an agent
    can be stopped before its request reaches the service. It must be told that,
    in words, as a refusal -- never as an empty result, and never as the
    service's own answer, which would send the reader looking in the wrong place."""
    monkeypatch.setattr(
        client,
        "_opener",
        _raises_with(
            403,
            {"Server": "cloudflare", "CF-Mitigated": "challenge"},
            b"<!DOCTYPE html><title>Just a moment...</title>",
        ),
    )
    with pytest.raises(client.HTTPFailure) as excinfo:
        client._request("GET", "https://example.invalid/api/v1/artifacts", "k", None)
    said = str(excinfo.value)
    assert said.startswith("REFUSED (HTTP 403)")
    assert "Cloudflare's bot protection challenged this request" in said
    assert "Just a moment" not in said  # say what it was, never what it contained


def test_any_other_cloudflare_page_is_attributed_to_cloudflare(monkeypatch):
    monkeypatch.setattr(
        client,
        "_opener",
        _raises_with(403, {"Server": "cloudflare"}, b"<html>blocked</html>"),
    )
    with pytest.raises(client.HTTPFailure) as excinfo:
        client._request("GET", "https://example.invalid/api/v1/artifacts", "k", None)
    assert "Cloudflare answered" in str(excinfo.value.detail)
    assert "blocked" not in str(excinfo.value.detail)


def test_every_request_names_itself_never_as_urllib(monkeypatch):
    """23 Sep 2026: Cloudflare's Browser Integrity Check refuses urllib's default
    `Python-urllib/*` agent (403, error 1010), measured at the real edge. Every
    outside agent using these commands was turned away until they named
    themselves."""
    seen = []

    class _Records:
        def open(self, request, timeout=None):
            seen.append(request.get_header("User-agent"))

            class _R:
                def __enter__(s):
                    return s

                def __exit__(s, *exc):
                    return False

                def read(s):
                    return b"{}"

            return _R()

    monkeypatch.setattr(client, "_opener", _Records())
    client._request("GET", "https://example.invalid/api/v1/artifacts", "k", None)
    assert seen and seen[0].startswith("artifacts-client/")
    assert "urllib" not in seen[0].lower()


def test_matches_answers_same_differs_and_refused_with_three_codes(
    live, key_file, tmp_path, capsys
):
    """The warpgate seat, 24 Sep 2026: comparing through `jq -r` added a newline
    and read as a mismatch. Exact bytes, and a refusal is never an answer."""
    path, _token = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html>\n<title>Board</title>\n<p>one</p>\n")
    assert client.main(["--key-file", str(path), str(page), "--title", "Board"]) == 0
    pid = json.loads(capsys.readouterr().out.strip())["id"]
    run = ["--key-file", str(path), "--matches", pid]

    assert client.main([*run, str(page)]) == 0
    assert "is the live version" in capsys.readouterr().err

    padded = tmp_path / "padded.html"
    padded.write_text(page.read_text() + "\n")
    assert client.main([*run, str(padded)]) == 3
    assert "only in trailing whitespace" in capsys.readouterr().err

    changed = tmp_path / "changed.html"
    changed.write_text(page.read_text().replace("one", "two"))
    assert client.main([*run, str(changed)]) == 3
    assert "first at line 3" in capsys.readouterr().err

    assert client.main([*run.copy()[:-1], "nosuchpage", str(page)]) == 1


def test_the_listing_names_its_columns_on_stderr_only(live, key_file, tmp_path, capsys):
    path, _token = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html>\n<title>Board</title>\n<p>x</p>\n")
    assert client.main(["--key-file", str(path), str(page), "--title", "Board"]) == 0
    capsys.readouterr()
    assert client.main(["--key-file", str(path), "--list"]) == 0
    out, err = capsys.readouterr()
    assert err.startswith("# id  title  address")
    assert not out.startswith("#"), "stdout stays rows and nothing else"


def test_preview_writes_the_png_and_never_claims_a_picture_it_lacks(
    live, key_file, tmp_path, capsys, monkeypatch
):
    from ream import thumbs

    path, _token = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><title>t</title><p>x</p>")
    shot = tmp_path / "shot.png"
    png = b"\x89PNG\r\n\x1a\n" + b"y" * 10
    monkeypatch.setattr(thumbs, "available", lambda: True)
    monkeypatch.setattr(thumbs, "render_report", lambda html, w, h: (png, []))
    run = ["--key-file", str(path), str(page), "--preview", str(shot)]
    assert client.main(run) == 0
    assert shot.read_bytes() == png
    assert "390x1600 preview" in capsys.readouterr().err

    monkeypatch.setattr(thumbs, "available", lambda: False)
    shot.unlink()
    assert client.main(run) == 3
    assert "no preview: the service has no browser" in capsys.readouterr().err
    assert not shot.exists()


def test_a_page_goes_over_the_wire_as_utf8_not_escapes(
    live, key_file, tmp_path, capsys
):
    """A \\u-escaped character is six bytes; a 3 MiB page must still fit the
    ingress's 4m limit as JSON (28 Sep 2026)."""
    path, _token = key_file
    page = tmp_path / "page.html"
    body = "<!doctype html>\n<title>Café</title>\n<p>naïve — 日本</p>\n"
    page.write_text(body, encoding="utf-8")
    assert client.main(["--key-file", str(path), str(page), "--title", "Café"]) == 0
    pid = json.loads(capsys.readouterr().out.strip())["id"]
    assert client.main(["--key-file", str(path), "--fetch", pid]) == 0
    assert capsys.readouterr().out == body
    import inspect

    assert "ensure_ascii=False" in inspect.getsource(client._request)


# --- folders (slice 1) ---------------------------------------------------------


def test_folder_on_publish_says_created_once_and_names_it_after(
    live, key_file, tmp_path, capsys
):
    path, _ = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    args = ["--key-file", str(path), str(page), "--title", "Board", "--folder", "road"]

    assert client.main(args) == 0
    first = capsys.readouterr()
    assert "created new folder 'road'" in first.err
    assert set(json.loads(first.out)) <= {
        "id",
        "page_url",
        "url",
        "revision",
        "title",
        "bytes",
    }, "stdout stays the whitelist: the folder is said on stderr"

    assert client.main(args) == 0
    second = capsys.readouterr()
    assert "created new folder" not in second.err
    assert "in folder 'road'" in second.err


def test_folders_and_list_folder_and_no_folder(live, key_file, tmp_path, capsys):
    path, _ = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    base = ["--key-file", str(path)]
    client.main([*base, str(page), "--title", "In", "--folder", "road"])
    inside = json.loads(capsys.readouterr().out)["id"]
    client.main([*base, str(page), "--title", "Out"])
    capsys.readouterr()

    assert client.main([*base, "--folders"]) == 0
    assert capsys.readouterr().out.strip().split("\t") == ["road", "road", "1", "0"]

    assert client.main([*base, "--list", "--folder", "road"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith(inside)

    assert client.main([*base, "--list", "--folder", "nonesuch"]) == 1
    assert "404" in capsys.readouterr().err

    page.write_text("<!doctype html><body>hello</body></html>")
    assert client.main([*base, str(page), "--update", inside, "--no-folder"]) == 0
    capsys.readouterr()
    client.main([*base, "--list", "--folder", "road"])
    assert capsys.readouterr().out.strip() == ""


def test_a_bad_folder_slug_is_refused_by_the_service_with_a_message(
    live, key_file, tmp_path, capsys
):
    path, _ = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    rc = client.main(
        ["--key-file", str(path), str(page), "--title", "B", "--folder", "Road Map"]
    )
    assert rc == 1
    assert "folder" in capsys.readouterr().err


def test_folder_alone_without_a_file_or_list_is_a_usage_error(live, key_file, capsys):
    path, _ = key_file
    assert client.main(["--key-file", str(path), "--folder", "road"]) == 2
    assert "--folder goes with FILE" in capsys.readouterr().err


def test_no_folder_and_all_misuse_are_usage_errors(live, key_file, tmp_path, capsys):
    path, _ = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    base = ["--key-file", str(path)]
    for extra in (
        [str(page), "--title", "T", "--no-folder"],
        [str(page), "--title", "T", "--folder", ""],
        ["--no-folder"],
    ):
        assert client.main([*base, *extra]) == 2
        assert "--no-folder" in capsys.readouterr().err
    assert client.main([*base, "--list", "--folder", "road", "--all"]) == 2
    assert "--all cannot be combined with --folder" in capsys.readouterr().err


# --- folders, bulk move (slice 2) ---------------------------------------------


def test_move_files_pages_and_says_created_and_missing(
    live, key_file, tmp_path, capsys
):
    path, _ = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    base = ["--key-file", str(path)]
    ids = []
    for i in range(2):
        client.main([*base, str(page), "--title", f"P{i}"])
        ids.append(json.loads(capsys.readouterr().out)["id"])

    assert client.main([*base, "--move", *ids, "nonesuch", "--folder", "road"]) == 0
    got = capsys.readouterr()
    assert json.loads(got.out) == {"moved": ids, "missing": ["nonesuch"]}
    assert "created new folder 'road'" in got.err

    assert client.main([*base, "--move", ids[0], "--folder", "road"]) == 0
    assert "in folder 'road'" in capsys.readouterr().err

    assert client.main([*base, "--move", *ids, "--no-folder"]) == 0
    assert json.loads(capsys.readouterr().out)["moved"] == ids
    assert client.main([*base, "--move", "nonesuch", "--no-folder"]) == 1


def test_move_misuse_is_a_usage_error(live, key_file, tmp_path, capsys):
    path, _ = key_file
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    base = ["--key-file", str(path), "--move", "abc"]
    for flag in ("--archive", "--unarchive", "--pin", "--unpin", "--duplicate"):
        assert client.main([*base, "--folder", "x", flag, "c"]) == 2
        assert flag in capsys.readouterr().err
    assert client.main([*base, "--folder", "x", "--rename", "c", "--title", "T"]) == 2
    assert "--rename" in capsys.readouterr().err
    assert client.main([*base, "--folder", "x", "--fetch", "c"]) == 2
    assert "--fetch" in capsys.readouterr().err
    assert client.main([*base, "--folder", "x", "--folders"]) == 2
    assert "--folders" in capsys.readouterr().err
    assert client.main([*base, "--folder", "x", "--check", str(page)]) == 2
    assert "--check" in capsys.readouterr().err
    for extra, word in (
        ([], "--move needs --folder"),
        (["--folder", "x", "--no-folder"], "cannot be used together"),
        (["--folder", ""], "--no-folder"),
        (["--no-folder", "--list"], "--move goes with"),
    ):
        assert client.main([*base, *extra]) == 2
        assert word in capsys.readouterr().err
    assert client.main([*base, "--folder", "x", str(page)]) == 2
    capsys.readouterr()


def test_the_top_usage_line_names_the_folder_flags(live, key_file, capsys):
    path, _ = key_file
    assert client.main(["--key-file", str(path)]) == 2
    err = capsys.readouterr().err
    assert "--move ID" in err and "--folder SLUG" in err and "--folders" in err


def test_move_folder_title_and_the_folder_rename_and_archive_flags(
    live, key_file, tmp_path, capsys
):
    path, _ = key_file
    base = ["--key-file", str(path)]
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><body>hi</body></html>")
    client.main([*base, str(page), "--title", "One"])
    one = json.loads(capsys.readouterr().out)["id"]
    moved = client.main(
        [*base, "--move", one, "--folder", "cps", "--folder-title", "Cove"]
    )
    assert moved == 0
    assert "created new folder 'cps'" in capsys.readouterr().err
    client.main([*base, "--folders"])
    assert capsys.readouterr().out.strip().split("\t")[:2] == ["cps", "Cove"]
    # title only
    assert client.main([*base, "--rename-folder", "cps", "--folder-title", "Cv"]) == 0
    assert "titled" in capsys.readouterr().err
    # slug
    assert client.main([*base, "--rename-folder", "cps", "--to", "split"]) == 0
    assert "'cps' to 'split'" in capsys.readouterr().err
    assert client.main([*base, "--list", "--folder", "cps"]) == 0
    assert capsys.readouterr().out.startswith(one)
    # nothing to change, and misuse
    assert client.main([*base, "--rename-folder", "split"]) == 2
    assert client.main([*base, "--archive-folder", "split", "--to", "x"]) == 2
    assert client.main([*base, "--confirm"]) == 2
    both = [*base, "--archive-folder", "a", "--unarchive-folder", "a"]
    assert client.main(both) == 2
    capsys.readouterr()
    # two steps
    assert client.main([*base, "--archive-folder", "cps"]) == 0
    out = capsys.readouterr()
    assert "NOT archived yet" in out.err and "would_archive" in out.out
    assert client.main([*base, "--archive-folder", "split", "--confirm"]) == 0
    assert "archived folder 'split': 1 page(s)" in capsys.readouterr().err
    assert client.main([*base, "--unarchive-folder", "split"]) == 0
    assert "1 page(s) restored" in capsys.readouterr().err
    assert client.main([*base, "--archive-folder", "ghost"]) == 1
    assert "404" in capsys.readouterr().err


# --- generated pages: --touch and the declared interval (slice 2) -------------


def _published(client_main, base, page, capsys, *extra):
    assert client_main([*base, str(page), "--title", "Board", *extra]) == 0
    return json.loads(capsys.readouterr().out)["id"]


def _page(tmp_path, text="hi"):
    page = tmp_path / "page.html"
    page.write_text(f"<!doctype html><body>{text}</body></html>")
    return page


def test_touch_says_checked_and_makes_no_version(live, key_file, tmp_path, capsys):
    path, _ = key_file
    base = ["--key-file", str(path)]
    aid = _published(client.main, base, _page(tmp_path), capsys)
    assert client.main([*base, "--touch", aid]) == 0
    got = capsys.readouterr()
    assert "checked, still current (changed " in got.err
    assert json.loads(got.out)["status"] == "ok"
    assert client.main([*base, "--versions", aid]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1, "no new version"


def test_touch_failed_with_a_note_says_so(live, key_file, tmp_path, capsys):
    path, _ = key_file
    base = ["--key-file", str(path)]
    aid = _published(client.main, base, _page(tmp_path), capsys)
    rc = client.main(
        [*base, "--touch", aid, "--status", "failed", "--note", "renderer exited 2"]
    )
    assert rc == 0
    got = capsys.readouterr()
    assert "marked failed: renderer exited 2" in got.err
    assert json.loads(got.out)["note"] == "renderer exited 2"


def test_touch_if_revision_refusal_exits_non_zero(live, key_file, tmp_path, capsys):
    path, _ = key_file
    base = ["--key-file", str(path)]
    page = _page(tmp_path)
    aid = _published(client.main, base, page, capsys)
    assert client.main([*base, "--touch", aid, "--if-revision", "1"]) == 0
    capsys.readouterr()
    page.write_text("<!doctype html><body>edited</body></html>")
    assert client.main([*base, str(page), "--update", aid]) == 0
    capsys.readouterr()
    assert client.main([*base, "--touch", aid, "--if-revision", "1"]) == 1
    assert "NOT vouched for" in capsys.readouterr().err


def test_touch_of_a_missing_page_exits_non_zero(live, key_file, capsys):
    path, _ = key_file
    assert client.main(["--key-file", str(path), "--touch", "nonesuch"]) == 1
    assert "404" in capsys.readouterr().err


def test_refresh_every_is_declared_on_publish_update_and_touch_and_cleared(
    live, key_file, tmp_path, capsys
):
    path, _ = key_file
    base = ["--key-file", str(path)]
    page = _page(tmp_path)
    aid = _published(client.main, base, page, capsys, "--refresh-every", "30m")

    def declared():
        assert client.main([*base, "--touch", aid]) == 0
        return json.loads(capsys.readouterr().out)["refresh_every"]

    assert declared() == 1800, "30m is 1800 seconds, kept by a plain touch"
    assert (
        client.main([*base, str(page), "--update", aid, "--refresh-every", "2h"]) == 0
    )
    capsys.readouterr()
    assert declared() == 7200
    assert client.main([*base, "--touch", aid, "--refresh-every", "1d"]) == 0
    assert json.loads(capsys.readouterr().out)["refresh_every"] == 86400
    assert client.main([*base, str(page), "--update", aid, "--no-refresh"]) == 0
    capsys.readouterr()
    assert declared() is None
    assert client.main([*base, "--touch", aid, "--refresh-every", "90"]) == 0
    assert client.main([*base, "--touch", aid, "--no-refresh"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["refresh_every"] is None


def test_a_refused_interval_exits_non_zero(live, key_file, tmp_path, capsys):
    path, _ = key_file
    base = ["--key-file", str(path)]
    aid = _published(client.main, base, _page(tmp_path), capsys)
    assert client.main([*base, "--touch", aid, "--refresh-every", "10s"]) == 1
    assert "refresh_every" in capsys.readouterr().err
    assert client.main([*base, "--touch", aid, "--refresh-every", "soon"]) == 2
    assert "refresh-every" in capsys.readouterr().err


def test_touch_misuse_is_a_usage_error(live, key_file, tmp_path, capsys):
    path, _ = key_file
    base = ["--key-file", str(path)]
    page = _page(tmp_path)
    assert client.main([*base, "--touch", "abc", str(page), "--update", "abc"]) == 2
    assert "--touch" in capsys.readouterr().err
    for flag in ("--pin", "--archive", "--rename", "--fetch"):
        assert client.main([*base, "--touch", "abc", flag, "x"]) == 2
        assert "--touch" in capsys.readouterr().err
    assert client.main([*base, "--status", "failed"]) == 2
    assert "--touch" in capsys.readouterr().err
    both = [*base, "--touch", "abc", "--refresh-every", "5m", "--no-refresh"]
    assert client.main(both) == 2
    assert client.main([*base, str(page), "--title", "T", "--no-refresh"]) == 2
    capsys.readouterr()
    assert client.main([*base, "--refresh-every", "5m"]) == 2


def test_parse_interval():
    assert client.parse_interval("90") == 90
    assert client.parse_interval("90s") == 90
    assert client.parse_interval("30m") == 1800
    assert client.parse_interval("2h") == 7200
    assert client.parse_interval("1d") == 86400
    for bad in ("", "m", "1.5h", "-5m", "5x", "h5"):
        with pytest.raises(client.UsageError):
            client.parse_interval(bad)
