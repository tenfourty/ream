"""Every place in `src/ream` that starts a secret scan is classified here, so that no scan
which can take a scan slot is reachable by accident.

A scan takes a slot only for a value longer than `scanslots.SMALL_PAGE`. Today that is the
page body alone. Every other scanned field is capped, before its scan, far below that. The
wall-clock backstop beside `scan.SCAN_CPU_SECONDS` is a slot-hold limit and is acceptable
only while a slot-taking scan needs a revocable credential; it must become a per-scan wall
bound if one becomes reachable without. A new call site fails this test until someone
says which kind it is.
"""

import ast
import importlib
import pathlib

import pytest

from ream import scanslots

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "ream"

# The scan itself and the helpers that reach it (`store._check_secrets` calls `scan.scan`;
# `_guard` and `check_body` call `_check_secrets`).
REACHES_THE_SCAN = {
    "scan.scan",
    "scan._scan",
    "_check_secrets",
    "store._check_secrets",
    "_guard",
    "store._guard",
    "check_body",
    "store.check_body",
}

BODY = "page body (may take a slot)"

# (file, enclosing function, the call, its first argument) -> BODY, or the cap that holds
# before the scan, as "module.CONSTANT" (asserted below to be under SMALL_PAGE).
SITES: dict[tuple[str, str, str, str], str] = {
    ("api.py", "preview", "store.check_body", "body"): BODY,
    ("client.py", "_check_page", "scan.scan", "body"): BODY,
    ("store.py", "_check_secrets", "scan.scan", "value"): "the helper itself",
    ("store.py", "check_body", "_check_secrets", "body"): BODY,
    ("store.py", "_guard", "_check_secrets", "value"): "see its callers",
    # `_guard`'s callers: the page body, or a title of at most MAX_TITLE_BYTES.
    ("store.py", "publish", "_guard", "body"): BODY,
    ("store.py", "publish", "_guard", "title"): "store.MAX_TITLE_BYTES",
    ("store.py", "_check_folder", "_guard", "folder_title"): "store.MAX_TITLE_BYTES",
    (
        "store.py",
        "file_page_for_reader",
        "_guard",
        "new_title",
    ): "store.MAX_TITLE_BYTES",
    (
        "store.py",
        "file_pages_for_reader",
        "_guard",
        "new_title",
    ): "store.MAX_TITLE_BYTES",
    ("store.py", "rename", "_guard", "title"): "store.MAX_TITLE_BYTES",
    ("store.py", "rename", "_check_secrets", "title"): "store.MAX_TITLE_BYTES",
    ("store.py", "patch", "_check_secrets", "body"): BODY,
    ("store.py", "patch", "_check_secrets", "title"): "store.MAX_TITLE_BYTES",
    ("store.py", "touch", "_check_secrets", "note"): "store.CHECK_NOTE_MAX",
    ("store.py", "page_note", "_check_secrets", "cleaned"): "store.SECTION_NOTE_MAX",
    (
        "store.py",
        "patch_section",
        "_check_secrets",
        "stored_text",
    ): "store.SECTION_NOTE_MAX",
    (
        "comments.py",
        "add",
        "store._check_secrets",
        "body",
    ): "comments.MAX_COMMENT_BYTES",
    (
        "comments.py",
        "add",
        "store._check_secrets",
        "effective_quote",
    ): "comments.MAX_QUOTE_BYTES",
    (
        "decisions.py",
        "ask",
        "store._check_secrets",
        "question",
    ): "decisions.MAX_QUESTION_BYTES",
    (
        "decisions.py",
        "ask",
        "store._check_secrets",
        "option['label']",
    ): "decisions.MAX_LABEL_BYTES",
    (
        "decisions.py",
        "ask",
        "store._check_secrets",
        "option['preview']",
    ): "decisions.MAX_PREVIEW_BYTES",
    (
        "decisions.py",
        "ask",
        "store._check_secrets",
        "value",
    ): "decisions.MAX_CONTEXT_CHARS",
    (
        "decisions.py",
        "release_round",
        "store._check_secrets",
        "reason",
    ): "decisions.MAX_RELEASE_REASON_BYTES",
    (
        "decisions.py",
        "record_elsewhere",
        "store._check_secrets",
        "note",
    ): "decisions.MAX_NOTE_BYTES",
    (
        "decisions.py",
        "answer",
        "store._check_secrets",
        "note",
    ): "decisions.MAX_NOTE_BYTES",
    (
        "decisions.py",
        "record_outcome",
        "store._check_secrets",
        "body",
    ): "decisions.MAX_OUTCOME_BYTES",
    (
        "formanswer.py",
        "relay",
        "store._check_secrets",
        "note",
    ): "decisions.MAX_NOTE_BYTES",
    ("formanswer.py", "relay", "store._check_secrets", "value"): "forms.MAX_MAXLENGTH",
    ("formanswer.py", "submit", "store._check_secrets", "v"): "forms.MAX_MAXLENGTH",
    (
        "partner_in.py",
        "answer_here",
        "store._check_secrets",
        "note",
    ): "decisions.MAX_NOTE_BYTES",
    (
        "readside.py",
        "answer_here",
        "store._check_secrets",
        "note",
    ): "decisions.MAX_NOTE_BYTES",
    ("receipts.py", "declare", "store._check_secrets", "body"): "receipts.MAX_NOTE",
}
# Where one site holds two fields of different caps, the larger is the one named above;
# `decisions.ask` also scans `blocked_on` and the topic title through the `value` loop, whose
# caps (MAX_BLOCKED_ON_CHARS, MAX_TOPIC_TITLE_CHARS) are below MAX_CONTEXT_CHARS.
# `receipts.declare` scans a note (MAX_NOTE) and a link (MAX_LINK) through one call.
ALSO_CAPPED = (
    "decisions.MAX_BLOCKED_ON_CHARS",
    "decisions.MAX_TOPIC_TITLE_CHARS",
    "receipts.MAX_LINK",
)
NOT_A_CAP = {BODY, "the helper itself", "see its callers"}


def _found() -> set[tuple[str, str, str, str]]:
    found = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        parent = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            if name not in REACHES_THE_SCAN or not node.args:
                continue
            up = node
            while up in parent and not isinstance(
                up, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                up = parent[up]
            found.add(
                (path.name, getattr(up, "name", "?"), name, ast.unparse(node.args[0]))
            )
    return found


def test_every_call_that_starts_a_scan_is_classified():
    found = _found()
    assert found, "the walk found no call site: it is broken, not the code clean"
    assert found - set(SITES) == set(), (
        "a new call that reaches the secret scan: say in SITES whether it is the page "
        "body (may take a slot) or a field capped below SMALL_PAGE before its scan"
    )
    assert set(SITES) - found == set(), "a classified call site that no longer exists"


@pytest.mark.parametrize(
    "cap", sorted({v for v in SITES.values() if v not in NOT_A_CAP} | set(ALSO_CAPPED))
)
def test_every_capped_field_is_capped_below_a_slot(cap):
    module, constant = cap.split(".")
    value = getattr(importlib.import_module(f"ream.{module}"), constant)
    assert value < scanslots.SMALL_PAGE, f"{cap} could take a scan slot"
    assert value <= 64 * 1024, f"{cap} is over 64 KiB"


def test_only_page_bodies_may_take_a_slot():
    bodies = {key for key, kind in SITES.items() if kind == BODY}
    assert {key[3] for key in bodies} == {"body"}
