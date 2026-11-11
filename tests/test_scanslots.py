"""Only so many scans of a large page run at once in one server process, and when they
are all taken a request waits a short while, then answers as each path was designed to:
the publish checks and the render findings say they did not run, a reader a request
needs answers 503 with `Retry-After`. A slot is always given back."""

import base64
import contextlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from ream import (
    anchors,
    envnames,
    forms,
    keys,
    renderfind,
    scanslots,
    sections,
    settings,
    store,
    text,
    thumbs,
)
from ream.app import create_app, create_gallery_app

HEAD = "<!doctype html><html><head><title>P</title></head><body>"
TAIL = "</body></html>"
BIG = scanslots.SMALL_PAGE + 1
FILLER = "<p>filler</p>" * 25_000  # more than SMALL_PAGE characters
FORM = '<form data-artifact-form="f"><label>Why <input type="text" name="why"></label></form>'
PAGE = HEAD + FILLER + '<div id="x">hi</div>' + TAIL
FORM_PAGE = HEAD + FILLER + FORM + TAIL
# A tag with more attributes than the parser reads: raised from inside the slot.
BUSY_TAG = '<form data-artifact-form="f" x="1" ' + "a " * 5000 + ">"
UNREADABLE = HEAD + FILLER + BUSY_TAG + TAIL
SLOTS = 2

assert len(PAGE) > scanslots.SMALL_PAGE and len(FORM_PAGE) > scanslots.SMALL_PAGE


@pytest.fixture(autouse=True)
def _two_slots_and_a_short_wait(monkeypatch):
    monkeypatch.setattr(scanslots, "WAIT_SECONDS", 0.05)
    with settings.override(scan_slots=str(SLOTS)):
        yield
    assert scanslots.in_use() == 0, "a slot was not given back"


@contextlib.contextmanager
def taken(count: int = SLOTS):
    """`count` slots held by threads of their own until the block ends. The test's own
    thread must not hold one: a thread that holds a slot takes no second."""
    release = threading.Event()
    held = threading.Semaphore(0)

    def hold() -> None:
        with scanslots.slot(BIG, wait=10):
            held.release()
            release.wait(30)

    threads = [threading.Thread(target=hold) for _ in range(count)]
    for thread in threads:
        thread.start()
    try:
        for _ in threads:
            assert held.acquire(timeout=10)
        assert scanslots.in_use() == count
        yield
    finally:
        release.set()
        for thread in threads:
            thread.join(10)


@pytest.fixture()
def full():
    with taken():
        yield


# --- the slots ---------------------------------------------------------------------


def _from_the_environment() -> int:
    """`scan_slot_count` of the variables alone, past the override the fixture sets."""
    return settings.Settings(scan_slots=settings._env("scan_slots")).scan_slot_count()


def test_the_setting_is_one_field_under_both_names(monkeypatch):
    monkeypatch.delenv("REAM_SCAN_SLOTS", raising=False)
    monkeypatch.delenv("ARTIFACTS_SCAN_SLOTS", raising=False)
    assert _from_the_environment() == settings.DEFAULT_SCAN_SLOTS == 4
    assert settings.current().scan_slot_count() == SLOTS  # the fixture's override
    monkeypatch.setenv("REAM_SCAN_SLOTS", "7")
    assert _from_the_environment() == 7
    monkeypatch.delenv("REAM_SCAN_SLOTS")
    monkeypatch.setenv("ARTIFACTS_SCAN_SLOTS", "3")
    assert _from_the_environment() == 3
    monkeypatch.setenv("REAM_SCAN_SLOTS", "5")
    with pytest.raises(envnames.EnvConflict):
        _from_the_environment()


@pytest.mark.parametrize("value", ["0", "-1", "two", "1.5"])
def test_a_slot_count_that_is_not_a_positive_whole_number_is_refused_at_start(value):
    with settings.override(scan_slots=value):
        found = settings.current().problems()
        assert "REAM_SCAN_SLOTS must be a positive whole number" in found
        # and a scan meanwhile is never an error: the default
        assert settings.current().scan_slot_count() == settings.DEFAULT_SCAN_SLOTS


def test_no_more_scans_than_slots_run_at_once():
    lock = threading.Lock()
    running = peak = 0
    failures: list[BaseException] = []

    def scan() -> None:
        nonlocal running, peak
        try:
            with scanslots.slot(BIG, wait=30):
                with lock:
                    running += 1
                    peak = max(peak, running)
                time.sleep(0.05)
                with lock:
                    running -= 1
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=scan) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert failures == []
    assert peak == SLOTS


def test_when_every_slot_is_taken_a_large_page_waits_then_gives_up(full):
    started = time.monotonic()
    with pytest.raises(scanslots.ScanBusy) as busy, scanslots.slot(BIG):
        raise AssertionError("no slot was free")
    assert 0.04 <= time.monotonic() - started < 5
    assert busy.value.retry_after == scanslots.RETRY_AFTER_SECONDS == 5
    assert str(busy.value) == scanslots.BUSY
    assert scanslots.in_use() == SLOTS, "a refused wait takes nothing"


def test_a_small_page_takes_no_slot(full):
    with scanslots.slot(scanslots.SMALL_PAGE):
        assert scanslots.in_use() == SLOTS
    with scanslots.slot(0):
        pass
    assert (
        len(forms.parse(HEAD + FORM + TAIL).forms) == 1
    )  # a small form page, read now
    assert text.visible("<p>hello</p>")[0] == "hello"


def test_a_scan_waits_for_a_slot_and_gets_it_when_one_is_given_back():
    release = threading.Event()
    held = threading.Event()

    def hold() -> None:
        # a second inside the first takes nothing more
        with scanslots.slot(BIG), scanslots.slot(BIG):
            held.set()
            release.wait(10)

    others = [threading.Thread(target=hold) for _ in range(SLOTS)]
    for thread in others:
        thread.start()
    assert held.wait(10)
    timer = threading.Timer(0.2, release.set)
    timer.start()
    started = time.monotonic()
    with scanslots.slot(BIG, wait=10):
        assert time.monotonic() - started < 5
    for thread in others:
        thread.join(10)
    timer.join()


def test_a_scan_inside_a_scan_takes_no_second_slot():
    with settings.override(scan_slots="1"):
        with scanslots.slot(BIG):
            with scanslots.slot(BIG, wait=0):
                assert scanslots.in_use() == 1
            assert scanslots.in_use() == 1
        assert scanslots.in_use() == 0


def test_a_slot_is_given_back_when_the_scan_raises():
    with settings.override(scan_slots="1"):
        with pytest.raises(KeyError), scanslots.slot(BIG):
            raise KeyError("x")
        assert scanslots.in_use() == 0
        # raised inside a scan inside a scan
        with pytest.raises(KeyError), scanslots.slot(BIG), scanslots.slot(BIG):
            raise KeyError("x")
        assert scanslots.in_use() == 0
        with scanslots.slot(BIG, wait=0):  # free again at once
            assert scanslots.in_use() == 1


@pytest.mark.parametrize(
    "read",
    [
        lambda: forms.parse(UNREADABLE),
        lambda: text.visible(UNREADABLE),
        lambda: sections.locate(UNREADABLE, "x"),
        lambda: anchors._headings(UNREADABLE),
    ],
    ids=["forms", "text", "sections", "anchors"],
)
def test_a_page_the_reader_gives_up_on_gives_its_slot_back(read):
    try:
        read()
    except Exception as exc:  # noqa: BLE001 - each says it its own way
        assert not isinstance(exc, scanslots.ScanBusy)
    assert scanslots.in_use() == 0


# --- each path when every slot is taken ----------------------------------------------


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def auth(db):
    token, _ = keys.mint(db, agent="seat")
    return {"Authorization": f"Bearer {token}"}


def publish(api, auth, body):
    return api.post(
        "/api/v1/pages",
        json={"title": "Board", "body": body, "comments": "off"},
        headers=auth,
    )


def test_the_publish_checks_say_they_did_not_run_and_the_page_is_published(
    api, auth, db
):
    clear = publish(api, auth, PAGE)
    assert clear.status_code == 201
    assert {w["shape"] for w in clear.json()["warnings"]}.isdisjoint(
        {"csp_checks_not_run", "trap_checks_not_run"}
    )
    with taken():
        busy = publish(api, auth, PAGE)
    assert busy.status_code == 201
    warnings = busy.json()["warnings"]
    assert sorted(w["shape"] for w in warnings) == [
        "csp_checks_not_run",
        "trap_checks_not_run",
    ], (
        "the two warnings a page too large for the checks gets, and nothing that reads as clean"
    )
    assert all("busy" in w["hint"] for w in warnings)
    assert len(store.list_for_agent(db, "seat")) == 2


def test_a_small_page_is_checked_though_every_slot_is_taken(api, auth, full):
    small = publish(
        api, auth, HEAD + "<script src='https://x.example/a.js'></script>" + TAIL
    )
    assert small.status_code == 201
    shapes = {w["shape"] for w in small.json()["warnings"]}
    assert "remote_script" in shapes and "csp_checks_not_run" not in shapes


def test_a_form_page_is_a_503_with_retry_after_and_stores_nothing(api, auth, db):
    with taken():
        busy = publish(api, auth, FORM_PAGE)
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "5"
    assert busy.json() == {"detail": scanslots.BUSY}
    assert store.list_for_agent(db, "seat") == []
    assert publish(api, auth, FORM_PAGE).status_code == 201, "and once a slot is free"


def test_the_mcp_tool_says_so_and_stores_nothing(db):
    host = "artifacts.tenfourty.site"
    token, _ = keys.mint(db, agent="mcp-seat")

    def call(body: str) -> dict:
        return client.post(
            "/mcp",
            headers={
                "host": host,
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "publish_page",
                    "arguments": {"title": "Board", "body": body, "comments": "off"},
                },
            },
        ).json()["result"]

    with settings.override(mcp_public_url=f"https://{host}"):
        client = TestClient(create_app(db), follow_redirects=False)
        with taken():
            busy = call(FORM_PAGE)
        assert busy["isError"] is True
        assert busy["content"][0]["text"] == scanslots.BUSY
        assert store.list_for_agent(db, "mcp-seat") == []
        assert call(FORM_PAGE)["isError"] is False


def test_the_readers_a_request_needs_raise_busy_for_a_large_page(full):
    for read in (
        lambda: forms.parse(FORM_PAGE),
        lambda: text.visible(PAGE),
        lambda: text.readable(PAGE),
        lambda: sections.locate(PAGE, "x"),
        lambda: sections.locate_many(PAGE, {"x"}),
        lambda: anchors._headings(PAGE),
    ):
        with pytest.raises(scanslots.ScanBusy):
            read()


def test_the_browser_faithful_section_check_takes_a_slot_whatever_the_size(full):
    small = HEAD + '<div id="x">hi</div>' + TAIL
    with pytest.raises(scanslots.ScanBusy):
        sections.check_target(small, "x")
    with pytest.raises(scanslots.ScanBusy):
        sections.verify(small, small.replace("hi", "ho"), "x", "replace")


def test_the_render_findings_leave_the_page_unread_and_unchecked(full):
    assert thumbs.scan_for_findings("<p>hi</p>")[0] == [], "a small page is read"
    missing, live = thumbs.scan_for_findings(PAGE)
    assert (missing, live) == (None, None)
    verdict, findings = renderfind.assess(
        [], {"text": 9, "media": 0, "height": 9, "scroll": 1, "view": 9}, missing, live
    )
    assert verdict == "unchecked", "never ok"
    assert {"kind": "unchecked", "detail": renderfind.UNREAD_NOTE} in findings


def test_the_render_findings_are_read_when_a_slot_is_free():
    missing, live = thumbs.scan_for_findings(PAGE)
    assert missing == [] and live is False


def test_a_preview_says_unchecked_when_the_findings_could_not_be_read(
    api, auth, monkeypatch
):
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 20
    report = (png, [])
    monkeypatch.setattr(thumbs, "available", lambda: True)
    monkeypatch.setattr(thumbs, "render_report", lambda html, w, h: report)
    with taken():
        got = api.post("/api/v1/preview", json={"body": PAGE}, headers=auth)
    assert got.status_code == 200
    body = got.json()
    assert base64.b64decode(body["png_base64"]) == png
    assert body["verdict"] == "unchecked"
    assert {"kind": "unchecked", "detail": renderfind.UNREAD_NOTE} in body["findings"]


def test_the_background_render_stores_unchecked_when_the_findings_could_not_be_read(
    db, monkeypatch
):
    saved = {}
    monkeypatch.setattr(thumbs, "available", lambda: True)
    monkeypatch.setattr(thumbs, "too_big_to_render", lambda html: False)
    monkeypatch.setattr(thumbs, "render_report", lambda html: (None, []))
    monkeypatch.setattr(
        store, "save_render_errors", lambda *args: saved.setdefault("args", args)
    )
    done = threading.Event()
    real = thumbs.scan_for_findings

    def scan(html):
        try:
            return real(html)
        finally:
            done.set()

    monkeypatch.setattr(thumbs, "scan_for_findings", scan)
    monkeypatch.setattr(thumbs, "render_in_background", _original_render_in_background)
    with taken():
        thumbs.render_in_background(db, "a" * 26, 1, PAGE)
        assert done.wait(10)
        deadline = time.monotonic() + 10
        while "args" not in saved and time.monotonic() < deadline:
            time.sleep(0.01)
    _, _, _, _, verdict, findings = saved["args"]
    assert verdict == "unchecked"
    assert {"kind": "unchecked", "detail": renderfind.UNREAD_NOTE} in findings


_original_render_in_background = thumbs.render_in_background
