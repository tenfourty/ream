"""Only so many scans of a large page run at once in one server process, and when they
are all taken a request waits a short while, then answers as each path was designed to:
the publish checks and the render findings say they did not run, a comment listing
leaves its pins' states unknown, a reader a request needs answers 503 with
`Retry-After`. A slot is always given back."""

import base64
import contextlib
import logging
import threading
import time
import types

import pytest
from fastapi.testclient import TestClient

from ream import (
    anchors,
    comments,
    envnames,
    forms,
    keys,
    renderfind,
    scan,
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
# Few tokens, so the form reading is quick even on a loaded machine: one long text node
# over SMALL_PAGE, not tens of thousands of elements for the parser to count.
FORM_PAGE = HEAD + "<p>" + "x" * BIG + "</p>" + FORM + TAIL
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
    api, auth, db, monkeypatch
):
    # The secret scan is a gate and refuses a busy publish (tests below); here it runs
    # without a slot, so what is shown is the checks after it.
    monkeypatch.setattr(scan, "scan", scan._scan)
    clear = publish(api, auth, PAGE)
    assert clear.status_code == 201
    assert {w["shape"] for w in clear.json()["warnings"]}.isdisjoint(
        {"csp_checks_not_run", "trap_checks_not_run", "embedded_fonts_not_run"}
    )
    with taken():
        busy = publish(api, auth, PAGE)
    assert busy.status_code == 201
    warnings = busy.json()["warnings"]
    assert sorted(w["shape"] for w in warnings) == [
        "csp_checks_not_run",
        "embedded_fonts_not_run",
        "trap_checks_not_run",
    ], (
        "the three warnings a page too large for the checks gets, and nothing that reads as clean"
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


def test_a_form_page_is_a_503_with_retry_after_and_stores_nothing(
    api, auth, db, monkeypatch
):
    # The forms' own wait: the secret scan before it runs without a slot here.
    monkeypatch.setattr(scan, "scan", scan._scan)
    with taken():
        busy = publish(api, auth, FORM_PAGE)
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "5"
    assert busy.json() == {"detail": scanslots.BUSY}
    assert store.list_for_agent(db, "seat") == []
    again = publish(api, auth, FORM_PAGE)
    assert again.status_code == 201, f"and once a slot is free: {again.text[:300]}"


def test_the_secret_scan_waits_then_gives_up_on_a_large_body(full):
    started = time.monotonic()
    with pytest.raises(scanslots.ScanBusy):
        scan.scan(PAGE)
    assert 0.04 <= time.monotonic() - started < 5
    assert scanslots.in_use() == SLOTS, "a refused wait takes nothing"
    small = HEAD + "<p>42 issues open</p>" + TAIL
    assert scan.scan(small) == [], "a small body is scanned though every slot is taken"


def test_a_secret_scan_that_runs_past_its_time_bound_is_refused_not_clean(monkeypatch):
    monkeypatch.setattr(scan, "SCAN_CPU_SECONDS", -1.0)  # already spent
    with pytest.raises(scanslots.ScanBusy) as raised:
        scan.scan(PAGE)
    assert isinstance(raised.value, scan.ScanTimeout)
    assert scanslots.in_use() == 0, "the slot was given back"
    small = HEAD + "<p>42 issues open</p>" + TAIL
    with pytest.raises(
        scan.ScanTimeout
    ):  # a small body has no slot, but the same bound
        scan.scan(small)


def test_the_time_bound_is_also_checked_inside_one_shape(monkeypatch):
    """One shape with many matches on a body: the bound passes at the check before the
    shape, then runs out partway through its matches, so it is the check at each match
    that raises. Dropping `deadline` from `_shape_lines` makes this scan finish instead."""
    shape = next(item for item in scan.SHAPES if item[0] == "password_assignment")
    monkeypatch.setattr(scan, "SHAPES", [shape])
    body = "<p>" + "DB_PASSWORD=hunter2fake " * 100 + "</p>"
    assert [f.shape for f in scan._scan(body)] == ["password_assignment"]

    calls = []

    def clock() -> float:  # one tick per read: 1.0, 2.0, ...
        calls.append(None)
        return float(len(calls))

    deadline = scan._Deadline(
        10.5, 1e9
    )  # made on the real clocks, then the clock is swapped
    deadline.cpu_until = 10.5
    monkeypatch.setattr(
        scan,
        "time",
        types.SimpleNamespace(thread_time=clock, monotonic=lambda: 0.0),
    )
    with pytest.raises(scan.ScanTimeout):
        scan._scan(body, deadline=deadline)
    assert len(calls) == 11, (
        "read once before the shape, then once per match, then stopped"
    )


def _ticking_cpu(monkeypatch, wall_offset=None):
    """`scan.time` with a thread clock that gains one second per read, and a wall clock
    that is the real one plus `wall_offset[0]` (when given)."""
    ticks = []

    def thread_time() -> float:
        ticks.append(None)
        return float(len(ticks))

    def monotonic() -> float:
        return time.monotonic() + (wall_offset[0] if wall_offset else 0.0)

    monkeypatch.setattr(
        scan,
        "time",
        types.SimpleNamespace(thread_time=thread_time, monotonic=monotonic),
    )
    return ticks


def test_a_scan_that_uses_ten_cpu_seconds_is_refused_at_about_ten(monkeypatch):
    ticks = _ticking_cpu(monkeypatch)
    assert scan.SCAN_CPU_SECONDS == 10.0
    with pytest.raises(scan.ScanTimeout):
        scan.scan(PAGE)
    # One read to start, then one per shape: ten seconds of the thread's clock were spent
    # at the eleventh read, and the scan stopped at the next.
    assert 11 <= len(ticks) <= 13, len(ticks)
    assert scanslots.in_use() == 0, "the slot was given back"


def test_a_publish_whose_body_scan_uses_ten_cpu_seconds_is_a_503_and_stores_nothing(
    api, auth, db, monkeypatch
):
    """The CPU clock ticks only inside the scan of the body (a page over `SMALL_PAGE`),
    so a refusal can only be the body's: the title and other small scans before it read
    a clock that never moves and pass."""
    ticks = []
    inside = threading.local()
    real = scan.scan
    outcomes: list[tuple[int, str]] = []

    def thread_time() -> float:
        if getattr(inside, "big", False):
            ticks.append(None)
            return float(len(ticks))
        return 0.0

    def watched(text):
        inside.big = len(text) > scanslots.SMALL_PAGE
        try:
            found = real(text)
            outcomes.append((len(text), "clean"))
            return found
        except scan.ScanTimeout:
            outcomes.append((len(text), "timeout"))
            raise
        finally:
            inside.big = False

    with monkeypatch.context() as m:
        m.setattr(scan, "scan", watched)
        m.setattr(
            scan,
            "time",
            types.SimpleNamespace(thread_time=thread_time, monotonic=time.monotonic),
        )
        busy = publish(api, auth, PAGE)
    assert busy.status_code == 503
    assert busy.json() == {"detail": scanslots.TIMED_OUT}
    assert store.list_for_agent(db, "seat") == []
    assert outcomes[-1] == (len(PAGE), "timeout"), outcomes
    assert all(kind == "clean" for size, kind in outcomes[:-1]), outcomes
    assert 11 <= len(ticks) <= 13, len(ticks)


_KEYS = (
    "password=",
    "passwd=",
    "pass=",
    "token=",
    "secret=",
    "PrivateKey =",
    "PresharedKey =",
)
# The worst case measured in production (scan.py, beside SCAN_CPU_SECONDS), scaled down to
# about 1 MiB: the seven assignment shapes with placeholder values, each key written plainly
# and again split by a tag, so every shape matches in both views and finds nothing.
_UNIT = "".join(
    f"<p>{key}&lt;your-value&gt; {key[:2]}<i></i>{key[2:]}&lt;your-value&gt;</p>\n"
    for key in _KEYS
)
WORST_CASE = "<html><body>" + _UNIT * (1024 * 1024 // len(_UNIT)) + "</body></html>"


def test_four_worst_case_pages_scanned_at_once_on_real_clocks_all_pass(monkeypatch):
    count = 4
    assert len(WORST_CASE) > scanslots.SMALL_PAGE
    start = threading.Barrier(count, timeout=20)
    peak: list[int] = []
    # The count is read once in the barrier's action, which runs before any thread is
    # released, so no scan can have finished and freed its slot first.
    inside = threading.Barrier(
        count, action=lambda: peak.append(scanslots.in_use()), timeout=20
    )
    results: list[object] = []
    cpu: list[float] = []
    arrived = threading.local()
    real_check = scan._check_deadline

    def check(deadline):
        if not getattr(arrived, "yes", False):
            arrived.yes = True
            inside.wait()  # all four hold a slot here, or this raises
        real_check(deadline)

    monkeypatch.setattr(scan, "_check_deadline", check)

    def run() -> None:
        try:
            start.wait()
            began = time.thread_time()
            found = scan.scan(WORST_CASE)
            cpu.append(time.thread_time() - began)
            results.append(found)
        except BaseException as exc:  # noqa: BLE001 -- recorded and asserted below
            results.append(exc)

    with settings.override(scan_slots=str(count)):
        threads = [threading.Thread(target=run) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
    assert results == [[]] * count, results
    assert peak == [count], "all four held a slot at the same moment"
    # Scaled from this page to the largest body the service stores: still inside the bound.
    scaled = max(cpu) * (store.MAX_BODY_BYTES / len(WORST_CASE))
    assert len(cpu) == count and scaled < scan.SCAN_CPU_SECONDS, (cpu, scaled)


def test_four_scans_at_once_pass_a_wall_jump_past_the_cpu_bound(monkeypatch):
    """The slots run scans as threads that share the interpreter lock, so each one's wall
    time is several times its CPU time. Four threads are held inside their slots until all
    four are there, then the wall clock jumps past `SCAN_CPU_SECONDS` (as it would in
    production, where all four took 10 s of wall each): each is still within its own CPU
    time and the slot-hold backstop, so none is refused."""
    count = 4
    offset = [0.0]
    _ticking_cpu_wall = types.SimpleNamespace(
        thread_time=time.thread_time,
        monotonic=lambda: time.monotonic() + offset[0],
    )
    monkeypatch.setattr(scan, "time", _ticking_cpu_wall)
    inside = threading.Barrier(
        count,
        action=lambda: offset.__setitem__(0, 2 * scan.SCAN_CPU_SECONDS),
        timeout=20,
    )
    peak = []
    arrived = threading.local()
    real_check = scan._check_deadline

    def check(deadline):
        if not getattr(arrived, "yes", False):
            arrived.yes = True
            peak.append(scanslots.in_use())
            inside.wait()  # all four hold a slot here, or this raises
        real_check(deadline)

    monkeypatch.setattr(scan, "_check_deadline", check)
    results: list[object] = []

    def run() -> None:
        try:
            results.append(scan.scan(PAGE))
        except BaseException as exc:  # noqa: BLE001 -- recorded and asserted below
            results.append(exc)

    with settings.override(scan_slots=str(count)):
        threads = [threading.Thread(target=run) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
    assert results == [[]] * count, results
    assert offset[0] > scan.SCAN_CPU_SECONDS, "the wall clock passed the CPU bound"
    assert max(peak) == count, "all four ran at once, each holding a slot"


def test_the_wall_backstop_still_ends_a_scan_that_holds_its_slot_too_long(monkeypatch):
    offset = [0.0]
    monkeypatch.setattr(
        scan,
        "time",
        types.SimpleNamespace(
            thread_time=time.thread_time,
            monotonic=lambda: time.monotonic() + offset[0],
        ),
    )
    real_check = scan._check_deadline
    seen = []

    def check(deadline):
        if not seen:
            seen.append(None)
            offset[0] = scan.SCAN_WALL_SECONDS_PER_SLOT * SLOTS + 1
        real_check(deadline)

    monkeypatch.setattr(scan, "_check_deadline", check)
    with pytest.raises(scan.ScanTimeout):
        scan.scan(PAGE)
    assert scanslots.in_use() == 0


def test_a_scan_that_timed_out_does_not_say_the_server_is_busy(monkeypatch):
    monkeypatch.setattr(scan, "SCAN_CPU_SECONDS", -1.0)
    with pytest.raises(scan.ScanTimeout) as raised:
        scan.scan(PAGE)
    assert str(raised.value) == scanslots.TIMED_OUT
    assert "server" not in scanslots.TIMED_OUT
    assert raised.value.retry_after == scanslots.RETRY_AFTER_SECONDS


def test_a_publish_whose_secret_scan_ran_out_of_time_is_a_503_and_stores_nothing(
    api, auth, db, monkeypatch
):
    with monkeypatch.context() as m:
        m.setattr(scan, "SCAN_CPU_SECONDS", -1.0)
        busy = publish(api, auth, PAGE)
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "5"
    assert store.list_for_agent(db, "seat") == []
    assert publish(api, auth, PAGE).status_code == 201, (
        "and a retry inside the bound is scanned"
    )


def test_the_secret_scan_waits_for_a_slot_and_gets_it_when_one_is_given_back(
    monkeypatch,
):
    monkeypatch.setattr(scanslots, "WAIT_SECONDS", 10)
    asked: list[int | None] = []
    real = scanslots.slot

    @contextlib.contextmanager
    def slot(size=None, wait=None):
        asked.append(size)
        with real(size, wait):
            yield

    # Every slot held by other threads, given back after a moment: the scan waits for
    # one and then runs under it.
    release = threading.Event()
    held = threading.Semaphore(0)

    def hold() -> None:
        with real(BIG, wait=10):
            held.release()
            release.wait(10)

    others = [threading.Thread(target=hold) for _ in range(SLOTS)]
    for thread in others:
        thread.start()
    for _ in others:
        assert held.acquire(timeout=10)
    monkeypatch.setattr(scanslots, "slot", slot)
    timer = threading.Timer(0.2, release.set)
    timer.start()
    started = time.monotonic()
    assert scan.scan(PAGE) == []
    waited = time.monotonic() - started
    for thread in others:
        thread.join(10)
    timer.join()
    assert 0.1 <= waited < 5, "the scan waited for a slot to be given back"
    assert asked == [len(PAGE)]


def test_a_publish_the_secret_scan_could_not_read_is_a_503_and_stores_nothing(
    api, auth, db, caplog, monkeypatch
):
    caplog.set_level(logging.INFO, logger="ream.refusals")
    with taken():
        busy = publish(api, auth, PAGE)
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "5"
    assert busy.json() == {"detail": scanslots.BUSY}
    assert store.list_for_agent(db, "seat") == []
    assert "scan_failed" not in caplog.text, "a busy server is not a scanner defect"
    again = publish(api, auth, PAGE)
    assert again.status_code == 201, f"and once a slot is free: {again.text[:300]}"

    def broken(_text):
        raise RuntimeError("a scanner defect")

    monkeypatch.setattr(scan, "scan", broken)
    publish(api, auth, PAGE)
    assert "scan_failed" in caplog.text, "the log above was captured: a defect is in it"


def test_an_update_the_secret_scan_could_not_read_is_a_503_and_changes_nothing(
    api, auth, db
):
    first = publish(api, auth, PAGE)
    assert first.status_code == 201
    page, revision = first.json()["id"], first.json()["revision"]
    changed = PAGE.replace("hi</div>", "hello</div>")

    def update():
        return api.patch(
            f"/api/v1/pages/{page}",
            json={"body": changed, "expected_revision": revision},
            headers=auth,
        )

    with taken():
        busy = update()
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "5"
    assert store.get_body(db, "seat", page).body == PAGE
    assert update().status_code == 200


def test_a_preview_the_secret_scan_could_not_read_is_a_503(api, auth):
    with taken():
        busy = api.post("/api/v1/preview", json={"body": PAGE}, headers=auth)
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "5"
    assert busy.json() == {"detail": scanslots.BUSY}


def test_the_mcp_tool_says_so_and_stores_nothing(db, monkeypatch):
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
            unscanned = call(PAGE)  # the secret scan's wait
        assert unscanned["isError"] is True
        assert unscanned["content"][0]["text"] == scanslots.BUSY
        assert store.list_for_agent(db, "mcp-seat") == []
        # The forms' own wait: the secret scan before it runs without a slot here.
        monkeypatch.setattr(scan, "scan", scan._scan)
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
    # The secret scan runs without a slot here: a busy one refuses the preview (above).
    monkeypatch.setattr(scan, "scan", scan._scan)
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


# --- the comment listings ------------------------------------------------------------

PIN_QUOTE = "Cost per seat is flat across plans"
# A page over SMALL_PAGE with a pin on a heading in it: both its words and its headings
# are scanned for the pin's state, each under a slot.
PINNED_PAGE = (
    HEAD + '<h2 id="costs">Costs</h2><p>' + PIN_QUOTE + ".</p>" + FILLER + TAIL
)
PIN_ANCHOR = {
    "kind": "range",
    "path": "body>p:nth-of-type(1)",
    "start": 0,
    "end": len(PIN_QUOTE),
    "heading": "Costs",
    "heading_id": "costs",
}


@pytest.fixture()
def pinned(db):
    """A large page with one pinned comment on it, its words not kept from the write."""
    assert len(PINNED_PAGE) > scanslots.SMALL_PAGE
    page = store.publish(db, "seat", "Costs", PINNED_PAGE)
    comment = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="Why flat?",
        quote=PIN_QUOTE,
        anchor=PIN_ANCHOR,
    )
    comments._VISIBLE_CACHE.clear()
    yield page, comment
    comments._VISIBLE_CACHE.clear()


def _listings(db, page):
    return {
        "page": lambda: comments.list_for_artifact(db, page.id),
        "agent": lambda: comments.list_for_agent(db, "seat"),
        "wall": lambda: [c for g in comments.across_pages(db) for c in g.rows],
    }


@pytest.mark.parametrize("listing", ["page", "agent", "wall"])
def test_a_listing_holds_no_connection_while_it_waits_for_a_slot(
    db, pinned, monkeypatch, listing
):
    page, _ = pinned
    open_now, waited = [0], []
    connect = db.connect

    @contextlib.contextmanager
    def counted():
        with connect() as conn:
            open_now[0] += 1
            try:
                yield conn
            finally:
                open_now[0] -= 1

    real = scanslots.slot

    @contextlib.contextmanager
    def slot(size=None, wait=None):
        if size is not None and size > scanslots.SMALL_PAGE:
            waited.append(open_now[0])  # connections open as a slot is asked for
        with real(size, wait):
            yield

    monkeypatch.setattr(db, "connect", counted)
    monkeypatch.setattr(scanslots, "slot", slot)
    rows = _listings(db, page)[listing]()
    assert waited, "the listing scanned the page under a slot"
    assert waited == [0] * len(waited), (
        "a connection was open while a slot was asked for"
    )
    assert [r.anchor_state for r in rows] == ["current"]


@pytest.mark.parametrize("listing", ["page", "agent", "wall"])
def test_a_listing_answers_when_every_slot_is_taken_its_pins_unknown(
    db, pinned, listing
):
    page, comment = pinned
    read = _listings(db, page)[listing]
    with taken():
        busy = read()
    assert [r.id for r in busy] == [comment.id], "the comment is still listed"
    assert (busy[0].quote_state, busy[0].anchor_state, busy[0].anchor_note) == ("",) * 3
    assert busy[0].anchor_current == ""
    # Nothing unread was kept: with a slot free the same listing tells the state.
    assert [r.anchor_state for r in read()] == ["current"]


def test_a_listing_keeps_the_words_it_had_and_leaves_only_the_unread_html_unknown(
    db, pinned
):
    page, _ = pinned
    assert comments.list_for_artifact(db, page.id)[0].anchor_state == "current"
    with taken():  # the page's words are kept from that listing; its headings are not
        (row,) = comments.list_for_artifact(db, page.id)
    assert row.quote_state != "", "the kept words still tell the quote"
    assert (row.anchor_state, row.anchor_note, row.anchor_current) == ("", "", "")


def test_the_comment_routes_answer_200_when_every_slot_is_taken(api, auth, pinned):
    page, _ = pinned
    with taken():
        per_page = api.get(f"/api/v1/pages/{page.id}/comments", headers=auth)
        every = api.get("/api/v1/comments", headers=auth)
    for got in (per_page, every):
        assert got.status_code == 200, got.text[:300]
        (row,) = got.json()
        assert row["anchor_state"] == "" and row["quote_state"] == ""
    again = api.get(f"/api/v1/pages/{page.id}/comments", headers=auth)
    assert again.json()[0]["anchor_state"] == "current"


def test_a_reply_on_a_pin_is_written_with_its_state_unknown_when_every_slot_is_taken(
    api, auth, pinned
):
    page, root = pinned
    url = f"/api/v1/pages/{page.id}/comments"
    reply = {"body": "Flat since the pricing change.", "thread_root": root.id}
    headers = {**auth, "Idempotency-Key": "reply-1"}
    with taken():
        made = api.post(url, json=reply, headers=headers)
    assert made.status_code == 201, made.text[:300]
    assert made.json()["quote_state"] == "" and made.json()["anchor_state"] == ""
    # The reply is stored once, and a retry of the same key replays the reply as it
    # was answered; a later listing, with a slot free, tells the real state.
    again = api.post(url, json=reply, headers=headers)
    assert again.status_code == 201 and again.json() == made.json()
    listed = api.get(url, headers=auth).json()
    assert [c["id"] for c in listed].count(made.json()["id"]) == 1
    assert {c["anchor_state"] for c in listed} == {"current"}


def test_after_one_refused_slot_a_listing_leaves_the_other_large_pages_unread(
    db, pinned, monkeypatch
):
    first, _ = pinned
    second = store.publish(db, "seat", "Costs again", PINNED_PAGE)
    comments.add(
        db,
        artifact_id=second.id,
        author_kind="human",
        author="jeremy",
        body="Why flat?",
        quote=PIN_QUOTE,
        anchor=PIN_ANCHOR,
    )
    comments._VISIBLE_CACHE.clear()
    asked = []
    real = scanslots.slot

    @contextlib.contextmanager
    def counted(size=None, wait=None):
        if size is not None and size > scanslots.SMALL_PAGE:
            asked.append(size)
        with real(size, wait):
            yield

    monkeypatch.setattr(scanslots, "slot", counted)
    with taken():
        asked.clear()  # the holders asked too
        busy = comments.list_for_agent(db, "seat")
    assert len(busy) == 2 and {r.artifact_id for r in busy} == {first.id, second.id}
    assert len(asked) == 1, "one refused wait, then the rest were not tried"
    for row in busy:
        assert (row.quote_state, row.anchor_state) == ("", ""), "empty, never stale"
        assert (row.anchor_note, row.anchor_current) == ("", "")
    asked.clear()
    states = {
        r.artifact_id: r.anchor_state for r in comments.list_for_agent(db, "seat")
    }
    assert states == {first.id: "current", second.id: "current"}
    assert asked, "and with a slot free each page was read"
