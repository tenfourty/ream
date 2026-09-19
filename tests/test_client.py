"""`artifacts-publish`: a stdlib-only console script driven against the REAL app
over REAL HTTP (a uvicorn thread on an ephemeral loopback port), not TestClient --
the point of this client is that it works with nothing but the standard library and
a socket, so it is tested the way it will actually run. The loopback host must
answer as the gallery: `ARTIFACTS_GALLERY_ORIGIN` is set to this server's own
`http://127.0.0.1:<port>` so `hosts.is_gallery` (host, port stripped) matches the
`Host` header the client's own requests carry -- no test-only hook, just the same
environment-driven read `create_app`'s router already uses in `test_router.py`.
"""

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
