"""A redraw that changes only text the agent marked volatile mints no version (23 Nov 2026).

`<!--ream:volatile-->` ... `<!--/ream:volatile-->` wraps text an agent retypes on every redraw (a
time, a counter). `store.patch` compares the live page and the new body with those spans cut out
and, when they match, takes the unchanged branch: the old bytes stay live. Runs on both databases."""

import pytest
from test_mcp import call_tool, client, headers, on, token, unfence_all  # noqa: F401

from ream import store

OPEN = "<!--ream:volatile-->"
CLOSE = "<!--/ream:volatile-->"
SECRET = "AKIA" + "IOSFODNN7EXAMPLE"
AGENT = "mcp-seat"


def page(clock: str = "10:00", rest: str = "green") -> str:
    return (
        "<!doctype html><html><head><title>Board</title></head><body>"
        f'<p id="status">{rest}</p>'
        f'<p id="clock">{OPEN}{clock}{CLOSE}</p>'
        "</body></html>"
    )


def put(db, body: str) -> str:
    return store.publish(db, AGENT, "Board", body).id


def update(db, page_id: str, body: str, **kw) -> store.Published:
    live = store.get_owned(db, AGENT, page_id)
    return store.patch(db, AGENT, page_id, body, live.revision, **kw)


def versions(db, page_id: str) -> int:
    return store.get_owned(db, AGENT, page_id).current_version


# --- the comparison on its own -----------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "same"),
    [
        ("a", "a", True),
        ("a", "b", False),
        (f"a{OPEN}1{CLOSE}b", f"a{OPEN}2{CLOSE}b", True),
        (f"a{OPEN}1{CLOSE}b", f"a{OPEN}1{CLOSE}c", False),
        (
            f"a{OPEN}1{CLOSE}b",
            "ab",
            True,
        ),  # a span added or dropped around nothing else
        (f"a{OPEN}1{CLOSE}b{OPEN}x{CLOSE}", f"a{OPEN}{CLOSE}b{OPEN}yy{CLOSE}", True),
        # Unbalanced or nested on either side: the bytes decide.
        (f"a{OPEN}1b", f"a{OPEN}2b", False),
        (f"a1{CLOSE}b", f"a2{CLOSE}b", False),
        (f"a{CLOSE}1{OPEN}b", f"a{CLOSE}2{OPEN}b", False),
        (f"a{OPEN}{OPEN}1{CLOSE}{CLOSE}b", f"a{OPEN}{OPEN}2{CLOSE}{CLOSE}b", False),
        (f"a{OPEN}1{CLOSE}b", f"a{OPEN}2{CLOSE}b{CLOSE}", False),
        (f"a{OPEN}1{CLOSE}b{OPEN}", f"a{OPEN}2{CLOSE}b{OPEN}", False),
    ],
)
def test_same_page(old, new, same):
    assert store._same_page(old, new) is same


def test_the_span_cap_is_200_on_either_side():
    def spans(n, text):
        return "".join(f"x{OPEN}{text}{CLOSE}" for _ in range(n))

    assert store._same_page(spans(200, "1"), spans(200, "2")) is True
    assert store._same_page(spans(201, "1"), spans(201, "2")) is False
    assert store._same_page(spans(200, "1"), spans(201, "1")) is False
    assert store._same_page(spans(201, "1"), spans(201, "1")) is True  # equal bytes


# --- store.patch ------------------------------------------------------------------------


def test_a_change_only_inside_a_span_mints_no_version(db):
    pid = put(db, page("10:00"))
    before = store.get_owned(db, AGENT, pid)
    answer = update(db, pid, page("10:05"))
    assert answer.unchanged and answer.volatile_ignored
    assert answer.version == before.current_version == 1
    assert answer.revision == before.revision
    # The old volatile text stays live; the new text was never stored.
    stored = store.get_body(db, AGENT, pid).body
    assert stored == page("10:00")
    assert "10:05" not in stored
    assert versions(db, pid) == 1


def test_an_identical_body_is_unchanged_but_not_volatile_ignored(db):
    pid = put(db, page())
    answer = update(db, pid, page())
    assert answer.unchanged and not answer.volatile_ignored


def test_a_change_outside_a_span_mints_a_version(db):
    pid = put(db, page("10:00", "green"))
    answer = update(db, pid, page("10:05", "red"))
    assert not answer.unchanged and not answer.volatile_ignored
    assert answer.version == 2
    # The new body is stored whole, its new volatile text included.
    assert store.get_body(db, AGENT, pid).body == page("10:05", "red")


@pytest.mark.parametrize(
    "marked",
    [
        lambda t: f"<p>{OPEN}{t}</p>",  # never closed
        lambda t: f"<p>{t}{CLOSE}</p>",  # never opened
        lambda t: f"<p>{CLOSE}{t}{OPEN}</p>",  # closed before it opens
        lambda t: f"<p>{OPEN}{OPEN}{t}{CLOSE}{CLOSE}</p>",  # nested
    ],
    ids=["no-close", "no-open", "reversed", "nested"],
)
def test_unbalanced_or_nested_tokens_are_decided_by_bytes(db, marked):
    def doc(t):
        return f"<!doctype html><html><body>{marked(t)}</body></html>"

    pid = put(db, doc("10:00"))
    answer = update(db, pid, doc("10:05"))
    assert not answer.unchanged and not answer.volatile_ignored
    assert versions(db, pid) == 2
    assert store.get_body(db, AGENT, pid).body == doc("10:05")


def test_over_200_spans_mint_a_version(db):
    def doc(n, t):
        return (
            "<!doctype html><html><body>"
            + "".join(f"<i>{OPEN}{t}{CLOSE}</i>" for _ in range(n))
            + "</body></html>"
        )

    pid = put(db, doc(200, "a"))
    assert update(db, pid, doc(200, "b")).volatile_ignored
    assert versions(db, pid) == 1
    big = put(db, doc(201, "a"))
    assert not update(db, big, doc(201, "b")).unchanged
    assert versions(db, big) == 2


def test_a_rename_with_a_volatile_only_change_still_renames(db):
    pid = put(db, page("10:00"))
    answer = update(db, pid, page("10:05"), title="Renamed")
    assert answer.unchanged and answer.volatile_ignored
    assert answer.title == "Renamed"
    assert store.get_owned(db, AGENT, pid).title == "Renamed"
    assert versions(db, pid) == 1


def test_the_check_is_still_recorded_and_refresh_every_applies(db):
    pid = put(db, page("10:00"))
    answer = update(db, pid, page("10:05"), refresh_every=3600)
    assert answer.volatile_ignored
    assert store.get_owned(db, AGENT, pid).refresh_every == 3600


def test_new_volatile_text_is_never_scanned(db):
    pid = put(db, page("10:00"))
    answer = update(db, pid, page(SECRET))
    assert answer.volatile_ignored
    assert SECRET not in store.get_body(db, AGENT, pid).body
    # Outside a span the same text is still refused.
    with pytest.raises(store.SecretFound):
        update(db, pid, page("10:00", SECRET))


def test_a_stored_page_is_never_the_stripped_text(db):
    pid = put(db, page("10:00"))
    update(db, pid, page("10:00", "red"))
    assert store.get_body(db, AGENT, pid).body == page("10:00", "red")


# --- the section path -------------------------------------------------------------------


def test_a_section_update_that_changes_only_a_span_mints_no_version(db):
    pid = put(db, page("10:00"))
    result = store.patch_section(
        db, AGENT, pid, "clock", "replace", f"{OPEN}10:05{CLOSE}"
    )
    assert result.published.unchanged and result.published.volatile_ignored
    assert versions(db, pid) == 1
    assert store.get_body(db, AGENT, pid).body == page("10:00")


def test_a_section_update_outside_a_span_mints_a_version(db):
    pid = put(db, page("10:00"))
    result = store.patch_section(db, AGENT, pid, "status", "replace", "red")
    assert not result.published.unchanged and not result.published.volatile_ignored
    assert versions(db, pid) == 2


# --- the answers ------------------------------------------------------------------------


def test_the_route_answer_carries_volatile_ignored(client, on, token, db):
    pid = put(db, page("10:00"))
    revision = store.get_owned(db, AGENT, pid).revision
    got = client.patch(
        f"/api/v1/pages/{pid}",
        headers=headers(token),
        json={"body": page("10:05"), "expected_revision": revision},
    )
    assert got.status_code == 200, got.text
    assert got.json()["volatile_ignored"] is True
    assert got.json()["unchanged"] is True
    changed = client.patch(
        f"/api/v1/pages/{pid}",
        headers=headers(token),
        json={"body": page("10:05", "red"), "expected_revision": revision},
    )
    assert changed.json()["volatile_ignored"] is False
    assert changed.json()["version"] == 2


def test_the_mcp_update_page_answer_carries_volatile_ignored(client, on, token, db):
    pid = put(db, page("10:00"))
    revision = store.get_owned(db, AGENT, pid).revision
    error, out = call_tool(
        client,
        token,
        "update_page",
        page_id=pid,
        body=page("10:05"),
        expected_revision=revision,
    )
    assert not error, out
    answer = unfence_all(out)
    assert answer["volatile_ignored"] is True and answer["unchanged"] is True
    assert answer["version"] == 1
    # The section form of the same tool.
    error, out = call_tool(
        client,
        token,
        "update_page",
        page_id=pid,
        section="clock",
        op="replace",
        body=f"{OPEN}10:06{CLOSE}",
    )
    assert not error, out
    assert unfence_all(out)["volatile_ignored"] is True
    assert versions(db, pid) == 1
