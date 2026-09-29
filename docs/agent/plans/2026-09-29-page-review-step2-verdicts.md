# Page review, step 2: review requests and verdicts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An agent asks Jeremy to review one of its pages. He answers *approved*, *approved with
notes*, *changes requested* or *dismissed*. The agent gets one numbered message that carries his
marks, plus an exit code it can act on without reading prose.

**Architecture:**
- A review request is a `decision` row with `kind = 'review'` and four fixed options, so the
  gallery's existing answer card, note field, acknowledgement, outcomes, events and wake-ups all
  carry over.
- A new `review.py` turns a verdict plus the page's open marks into the message and its JSON twin.
  That output is served at `GET /api/v1/reviews/{id}`.
- `artifacts-decide` gains `ask-review` and `wait --review`.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres, pytest against real Postgres
(conftest wires it). The CLI is stdlib only (`src/artifacts/decide.py`, `client.py`).

**Spec:** `docs/agent/specs/2026-09-28-page-review-design.md`, sections "What Jeremy sees" (the
verdict and stale-device parts), "What the agent does", "Data" (`decision.kind`), "Security" and
"Build order" step 2. Research: `docs/agent/research/2026-09-28-plannotator-study.md` §B and §C.

## Global Constraints

- **Ideas only from plannotator; no copied code** (Jeremy, 28 Sep 2026).
- **Kinds and verdicts.** `decision.kind` is exactly `question` or `review`, default `question`.
- **Fixed options for a review, in this order:**

  | letter | label | verdict |
  |---|---|---|
  | a | Approved | approved |
  | b | Approved with notes | approved_with_notes |
  | c | Changes requested | changes_requested |
  | d | Dismissed | dismissed |

  There is no recommended letter; the verdict is his.
- **Who may give a verdict.** An agent may **ask** for a review, **read** the verdict and
  **record** a relayed one with its source (the existing `/decisions/{id}/record`). It never
  answers one itself.
- **One verdict counts.** Once a review is answered, a second verdict is refused. The gallery
  answers **409**, the page says which verdict counted and when, and the API answers 409 with
  `{"field": "answer", "verdict": ..., "answered_at": ...}`. (Rounds, which reopen a review, are
  step 4.)
- **The message's first line:** `Artifacts review: <VERDICT IN CAPS> — "<title>" (<page-id>, v<n>)`.
  It never starts with `/`.
- **Firm and gentle wording, verbatim.**
  - Changes requested: `You MUST revise the page to address ALL N items below, then republish it with artifacts-publish <file> --update <page-id>. Do not republish it unchanged.`
  - Approved with notes: `These notes are non-blocking guidance. Do not revise solely because of them.`
  - Dismissed: `Closed without a verdict. This is not approval.`
  - Approved: `Approved.`
- **Items**, numbered from 1, in page order:
  - `N. Feedback on: "<quote>"` then `   > <note>`
  - `N. Remove this: "<quote>"`
  - `N. Looks good: "<quote>"`
  - `N. General feedback about the page` then `   > <note>`
  - Every item ends with ` (mark <id>)`.
  - Replies nest under their mark as `   - <author>: <text>`, with no numbers.
- **The header** reads `He left N marks:` (or `He left no marks.`), and N always equals the
  numbered items.
- **Sanitising.** Every human string (quotes, notes, replies, and his verdict note) loses
  U+202A–U+202E, U+2066–U+2069, U+200B–U+200F, U+2060 and U+FEFF. Quotes are collapsed to one
  line and cut to 300 characters.
- **The JSON twin** is
  `{"verdict","round":1,"version","base_version":null,"note","items":[{"n","id","kind","quote","note","state","replies":[{"author","text"}]}]}`.
  `state` is `quote_state`: `""`, `"current"` or `"changed"`.
- **Exit codes for `wait --review`:**

  | code | meaning |
  |---|---|
  | 0 | approved |
  | 4 | approved with notes |
  | 5 | changes requested |
  | 6 | dismissed |
  | 7 | new marks arrived, no verdict yet |
  | 3 | timeout (unchanged) |
  | 1 | refused (unchanged) |
  | 2 | usage (unchanged) |

  `--require-approval` maps 0 and 4 to 0, and 5, 6 and 7 to 1. Errors stay 1 or 2 and print
  `REFUSED`.
- `--result-file F` writes the same bytes as stdout, **after** stdout, atomically (temp file,
  then `os.link`), and never overwrites an existing file. If F exists, that is exit 2 with a
  message, checked before waiting.
- Closing a tab or a waiter timing out never means dismissed. Dismissed is only the button.
- Commits are conventional, with no Co-Authored-By line (the repo guard refuses one). Run
  `uv run ruff format src tests` and `uv run ruff check src tests` before each commit.

## Review Focus

1. **A verdict on a page with no marks.** The message says `He left no marks.`, the changes-
   requested wording says `ALL 0 items`, and the JSON has `items: []`. Pinned in Task 2.
2. **Bidi or zero-width characters, or a leading `/`, in a mark note or quote.** They are
   stripped, and the message still starts `Artifacts review:`. Pinned in Task 2.
3. **Two devices give a verdict.** The second gets 409, and the counted verdict is unchanged in
   the database. Pinned in Task 1 (model) and Task 3 (gallery and API).
4. **`wait --review` started after the verdict was already given.** It prints and exits
   immediately with the right code; it starts by looking. Pinned in Task 4.
5. **`--result-file` pointing at an existing file.** Exit 2 before any wait, and the file is
   untouched. Pinned in Task 4.

---

## File map

| File | Change |
|---|---|
| `src/artifacts/schema.sql` | `decision.kind` column |
| `src/artifacts/decisions.py` | `KINDS`, `REVIEW_OPTIONS`, `VERDICTS`, `DecisionRow.kind`, `ask_review()`, `AlreadyDecided`, and `answer()` refusing a second review verdict |
| `src/artifacts/review.py` (new) | `sanitize()` and `render(db, review_id, *, agent) -> tuple[str, dict]` |
| `src/artifacts/api.py` | `POST /artifacts/{id}/reviews` and `GET /reviews/{id}`; 409 for `AlreadyDecided` on record |
| `src/artifacts/gallery.py` | a review card label, a settled review card without buttons, and 409 on a second verdict |
| `src/artifacts/decide.py` | `ask-review` and `wait --review` with codes, `--json`, `--result-file`, `--require-approval`, `--verdict-only` |
| `src/artifacts/news.py`, `src/artifacts/onboarding.py` | a news entry and one line in the served skill's loop |
| `tests/test_reviews.py` (new) | model, renderer, API and gallery tests |
| `tests/test_decide_review.py` (new) | CLI tests against the live-server fixture |

---

### Task 1: a review is a kind of decision, and one verdict counts

**Files:**
- Modify: `src/artifacts/schema.sql`, beside the other `alter table decision add column` lines
  (around line 410).
- Modify: `src/artifacts/decisions.py`: `_COLUMNS` (line ~70), `DecisionRow` (~81),
  `_row_to_decision` (~179), a new `ask_review` after `ask` (~262), and `answer` (~444).
- Test: `tests/test_reviews.py`

**Interfaces:**
- Produces:
  - `decisions.KINDS = ("question", "review")`
  - `decisions.REVIEW_OPTIONS: list[dict]` (four `{"letter","label"}` entries, in order)
  - `decisions.VERDICTS: dict[str, str] = {"a": "approved", "b": "approved_with_notes", "c": "changes_requested", "d": "dismissed"}`
  - `DecisionRow.kind: str`
  - `decisions.ask_review(db, *, agent, artifact_id, session=None) -> DecisionRow`
  - `class decisions.AlreadyDecided(Exception)`, carrying `.row: DecisionRow` (the counted one)
    and `.field = "answer"`
  - `answer()` raises `AlreadyDecided` for an already-answered review.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reviews.py
"""Review requests and verdicts (page review step 2, homelab-ij8.108)."""

import pytest

from artifacts import decisions, events, store

PAGE = (
    "<!doctype html><title>Plan</title>"
    "<p>Rotate the keys every 90 days.</p><p>Email the team when done.</p>"
)


def _page(db, agent="seat"):
    return store.publish(db, agent, "Plan", PAGE)


def test_a_review_is_a_decision_with_four_fixed_verdicts(db):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    assert row.kind == "review"
    assert [o["label"] for o in row.options] == [
        "Approved", "Approved with notes", "Changes requested", "Dismissed"]
    assert row.recommended is None
    assert row.question.startswith("Review")


def test_an_ordinary_question_is_still_kind_question(db):
    page = _page(db)
    row = decisions.ask(db, agent="seat", artifact_id=page.id, question="q?",
                        options=[{"letter": "a", "label": "x"}, {"letter": "b", "label": "y"}])
    assert row.kind == "question"


def test_only_the_owner_can_ask_for_a_review(db):
    page = _page(db)
    with pytest.raises(store.NotFound):
        decisions.ask_review(db, agent="someone-else", artifact_id=page.id)


def test_the_first_verdict_counts_and_a_second_is_refused(db):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    decisions.answer(db, row.id, letter="c", by="jeremy")
    with pytest.raises(decisions.AlreadyDecided) as err:
        decisions.answer(db, row.id, letter="a", by="jeremy")
    assert err.value.row.answer == "c"
    assert err.value.field == "answer"
    [stored] = decisions.list_for_artifact(db, "seat", page.id)
    assert stored.answer == "c", "the counted verdict is unchanged"


def test_a_question_can_still_be_re_answered(db):
    page = _page(db)
    row = decisions.ask(db, agent="seat", artifact_id=page.id, question="q?",
                        options=[{"letter": "a", "label": "x"}, {"letter": "b", "label": "y"}])
    decisions.answer(db, row.id, letter="a", by="jeremy")
    assert decisions.answer(db, row.id, letter="b", by="jeremy").answer == "b"


def test_a_verdict_wakes_the_agent_like_an_answer(db):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    decisions.answer(db, row.id, letter="b", note="small things", by="jeremy")
    kinds = [e.kind for e in events.list_for_agent(db, "seat")]
    assert "decision_answered" in kinds
```

Before running, check `decisions.list_for_artifact`'s real signature (`grep -n "def list_for_artifact" -A6 src/artifacts/decisions.py`) and adjust the call if it differs.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest -q tests/test_reviews.py`
Expected: FAIL with `AttributeError: module 'artifacts.decisions' has no attribute 'ask_review'`.

- [ ] **Step 3: Implement**

In `schema.sql`:

```sql
-- A review request is a decision with four fixed verdicts (page review step 2,
-- homelab-ij8.108, 29 Sep 2026). 'question' is every lettered question before it.
alter table decision add column if not exists kind text not null default 'question'
    check (kind in ('question', 'review'));
```

In `decisions.py`:
- Append `, decision.kind` to the END of `_COLUMNS`, so it is the last column (index 18).
- Add `kind: str = "question"` as the last field of `DecisionRow`, and `kind=row[18]` in
  `_row_to_decision`.
- **Check every `row[-1]` / `row[-2]` and `len(_COLUMNS.split(","))` use in this file.**
  `answer()` computes `asking_session = row[len(_COLUMNS.split(",")) - 1]`, which would now
  point at `kind`. Change it to index the session column explicitly: `row[17]`, with a comment
  saying that `session` is the 18th column of `_COLUMNS` and `kind` the 19th. Search:
  `grep -n "row\[-\|split(\",\")" src/artifacts/decisions.py`.

Add, after `MAX_OPTIONS`:

```python
KINDS = ("question", "review")

# The four verdicts, in the order his buttons show them. A review asks
# nothing he must choose between: there is no recommended letter.
REVIEW_OPTIONS = [
    {"letter": "a", "label": "Approved"},
    {"letter": "b", "label": "Approved with notes"},
    {"letter": "c", "label": "Changes requested"},
    {"letter": "d", "label": "Dismissed"},
]
VERDICTS = {
    "a": "approved",
    "b": "approved_with_notes",
    "c": "changes_requested",
    "d": "dismissed",
}


class AlreadyDecided(Exception):
    """A second verdict on a review. The first one counts (page review design:
    'a verdict from a stale second device is refused, and he is told which
    verdict counted and when'). `row` is the review as it stands."""

    def __init__(self, row: "DecisionRow") -> None:
        super().__init__("answer")
        self.field = "answer"
        self.row = row
```

`ask_review`:

```python
def ask_review(
    db: Database, *, agent: str, artifact_id: str, session: str | None = None
) -> DecisionRow:
    """Ask Jeremy to review `artifact_id`: a decision of kind 'review' with the
    four fixed verdicts. Same ownership rule as `ask`: not yours and missing
    look identical."""
    with db.connect() as conn:
        owned = store._owned(conn, artifact_id, agent)
        if owned is None:
            raise store.NotFound()
        title, version_n = owned[1], owned[4]
        question = f"Review requested: {title}"[:MAX_QUESTION_BYTES]
        decision_id = ids.new_id()
        conn.execute(
            "insert into decision (id, artifact_id, version_n, question, options,"
            " recommended, session, kind)"
            " values (%s, %s, %s, %s, %s, null, %s, 'review')",
            (decision_id, artifact_id, version_n, question,
             Jsonb(REVIEW_OPTIONS), session),
        )
        created = conn.execute(
            f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant
            (decision_id,),
        ).fetchone()
    return _row_to_decision(created)
```

The title is the page's own stored title, which was scanned when it was published, so it needs
no second scan. Check that `store._owned`'s row has the title at index 1 and the current version
at index 4 (`grep -n "def _owned" -A12 src/artifacts/store.py`). If it does not, use the right
indexes.

In `answer()`, right after the letter check (`if letter not in letters: raise InvalidAnswer()`):

```python
        # One verdict counts on a review: a second device, or a stale form, is
        # refused and told what counted. A QUESTION may still be re-answered.
        if row[18] == "review" and row[6] is not None:
            raise AlreadyDecided(_row_to_decision(row[: len(_COLUMNS.split(","))]))
```

(`row[6]` is `answer`; the join appends title and agent after `_COLUMNS`, so slice them off
before `_row_to_decision`.)

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest -q tests/test_reviews.py tests/test_decisions*.py tests/test_uncollected.py`
Expected: PASS. The existing decision suites stay green because the default kind is `question`.

- [ ] **Step 5: Commit**

```bash
git add src/artifacts/schema.sql src/artifacts/decisions.py tests/test_reviews.py
git commit -m "feat(decisions): an agent can ask for a review, and the first verdict counts"
```

---

### Task 2: the review message and its data

**Files:**
- Create: `src/artifacts/review.py`
- Test: `tests/test_reviews.py` (append)

**Interfaces:**
- Consumes: `decisions.VERDICTS` and `DecisionRow.kind` (Task 1);
  `comments.list_for_artifact(db, artifact_id, agent=..., include_resolved=...)`, whose real
  signature you must check (`grep -n "def list_for_artifact" -A12 src/artifacts/comments.py`);
  `CommentRow.kind` and `quote_state`; `text.searchable(body)`, which gives a page's readable
  words.
- Produces:
  - `review.sanitize(s: str) -> str`
  - `review.render(db, review_id: str, *, agent: str) -> tuple[str, dict]`, which raises
    `store.NotFound` for a review the agent does not own, or one that is not a review, or one
    with no verdict yet.

- [ ] **Step 1: Write the failing tests**

```python
from artifacts import comments, review

RANGE1 = {"kind": "range", "path": "body>p:nth-of-type(1)", "start": 0, "end": 31}
RANGE2 = {"kind": "range", "path": "body>p:nth-of-type(2)", "start": 0, "end": 25}


def _reviewed(db, letter, note=""):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    return page, row, letter, note


def _answer(db, row, letter, note=""):
    decisions.answer(db, row.id, letter=letter, note=note, by="jeremy")


def test_changes_requested_is_firm_numbered_in_page_order_with_ids(db):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    second = comments.add(db, artifact_id=page.id, author_kind="human", author="jeremy",
                          body="", kind="remove", quote="Email the team when done.",
                          anchor=RANGE2)
    first = comments.add(db, artifact_id=page.id, author_kind="human", author="jeremy",
                         body="why 90? weekly", kind="comment",
                         quote="Rotate the keys every 90 days.", anchor=RANGE1)
    _answer(db, row, "c")
    message, data = review.render(db, row.id, agent="seat")
    lines = message.splitlines()
    assert lines[0] == f'Artifacts review: CHANGES REQUESTED — "Plan" ({page.id}, v1)'
    assert "You MUST revise the page to address ALL 2 items below" in message
    assert "Do not republish it unchanged." in message
    assert "He left 2 marks:" in message
    i1 = message.index(f'1. Feedback on: "Rotate the keys every 90 days." (mark {first.id})')
    i2 = message.index(f'2. Remove this: "Email the team when done." (mark {second.id})')
    assert i1 < i2, "page order, not creation order"
    assert "   > why 90? weekly" in message
    assert data["verdict"] == "changes_requested"
    assert [i["n"] for i in data["items"]] == [1, 2]
    assert data["items"][0]["id"] == first.id and data["items"][1]["kind"] == "remove"


def test_no_marks_still_says_so(db):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    _answer(db, row, "c")
    message, data = review.render(db, row.id, agent="seat")
    assert "He left no marks." in message
    assert "ALL 0 items" in message
    assert data["items"] == []


@pytest.mark.parametrize("letter,verdict,phrase", [
    ("a", "approved", "Approved."),
    ("b", "approved_with_notes", "These notes are non-blocking guidance. Do not revise solely because of them."),
    ("d", "dismissed", "Closed without a verdict. This is not approval."),
])
def test_each_verdict_has_its_own_wording(db, letter, verdict, phrase):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    _answer(db, row, letter, note="see below")
    message, data = review.render(db, row.id, agent="seat")
    assert phrase in message and data["verdict"] == verdict
    assert data["note"] == "see below" and "His note: see below" in message


def test_invisible_characters_and_a_leading_slash_are_stripped(db):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    comments.add(db, artifact_id=page.id, author_kind="human", author="jeremy",
                 body="/rm -rf‮​ evil", kind="comment",
                 quote="Rotate the keys every 90 days.", anchor=RANGE1)
    _answer(db, row, "c", note="﻿/also")
    message, data = review.render(db, row.id, agent="seat")
    assert message.startswith("Artifacts review:")
    for ch in "‮​﻿":
        assert ch not in message and ch not in str(data)


def test_replies_nest_unnumbered_and_resolved_marks_are_left_out(db):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    root = comments.add(db, artifact_id=page.id, author_kind="human", author="jeremy",
                        body="why?", kind="comment",
                        quote="Rotate the keys every 90 days.", anchor=RANGE1)
    comments.add(db, artifact_id=page.id, author_kind="agent", author="seat",
                 body="because", thread_root=root.id)
    settled = comments.add(db, artifact_id=page.id, author_kind="human", author="jeremy",
                           body="old", kind="comment",
                           quote="Email the team when done.", anchor=RANGE2)
    comments.resolve(db, settled.id, by="jeremy")
    _answer(db, row, "b")
    message, data = review.render(db, row.id, agent="seat")
    assert "   - seat: because" in message
    assert "He left 1 marks:" in message or "He left 1 mark:" in message
    assert settled.id not in message


def test_a_quote_is_one_line_and_at_most_300_characters():
    assert review.sanitize("a‮b") == "ab"
    long = review._quote("x " * 400)
    assert "\n" not in long and len(long) <= 300


def test_render_refuses_what_is_not_yours_or_not_decided(db):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    with pytest.raises(store.NotFound):
        review.render(db, row.id, agent="seat")  # no verdict yet
    _answer(db, row, "a")
    with pytest.raises(store.NotFound):
        review.render(db, row.id, agent="someone-else")
```

Check the real signatures of `comments.resolve` and `comments.list_for_artifact` first, and
adapt these calls to them. If a helper name differs, keep the test's intent.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest -q tests/test_reviews.py -k "render or verdict_has or invisible or replies or quote_is or no_marks or firm"`
Expected: FAIL (`ModuleNotFoundError: artifacts.review`).

- [ ] **Step 3: Implement** `src/artifacts/review.py`:

```python
"""The message an agent gets when Jeremy gives a verdict on a page it asked
him to review (page review step 2, homelab-ij8.108).

One message for the whole review, the verdict first, then every open mark on
the page as a numbered item in page order. The wording is plannotator's
lesson (docs/agent/research/2026-09-28-plannotator-study.md, section B):
agents ignore soft wording on "changes requested", and over-revise on
"approved with notes", so each verdict says exactly what to do. Ideas only;
no plannotator code (Jeremy, 28 Sep 2026).
"""

from __future__ import annotations

import re

from artifacts import comments, decisions, store, text
from artifacts.db import Database

# Bidi controls, zero-width characters and the BOM: a note that renders one
# way to Jeremy and another way to an agent is how an instruction hides.
_INVISIBLE = re.compile("[‪-‮⁦-⁩​-‏⁠﻿]")
QUOTE_MAX = 300

_HEAD = {
    "approved": "Approved.",
    "approved_with_notes": (
        "These notes are non-blocking guidance. Do not revise solely because of them."
    ),
    "dismissed": "Closed without a verdict. This is not approval.",
}
_LABEL = {"comment": "Feedback on", "remove": "Remove this", "looks_good": "Looks good"}


def sanitize(s: str) -> str:
    return _INVISIBLE.sub("", s or "")


def _quote(s: str) -> str:
    return " ".join(sanitize(s).split())[:QUOTE_MAX]


def _note(s: str) -> str:
    # Never let a line of his start the message or a line with "/": a leading
    # slash reads as a command to some agents (artifact-server's rule).
    return sanitize(s).strip()


def render(db: Database, review_id: str, *, agent: str) -> tuple[str, dict]:
    ...
```

`render` does four things:
1. It loads the decision through `decisions.list_for_artifact`, or a direct query with the same
   ownership rule. It raises `store.NotFound` unless the row is `kind == "review"`, is owned by
   `agent`, and has an answer.
2. It loads the page (title, current version and body, via the existing store helper that
   `/body` uses) and the page's **open root comments by a human** with their replies.
3. It orders them into page order. Range-anchored marks are sorted by the index of their
   collapsed quote in `text.searchable(body)`, lowercased; a quote that isn't found sorts after
   the found ones. Page-level comments come last. Ties keep creation order.
4. It builds `lines` exactly as the Global Constraints specify:
   - line 1: `Artifacts review: {VERDICT} — "{title}" ({page_id}, v{version})`, with the
     verdict upper-cased and `_` turned into a space;
   - a blank line;
   - the verdict text. For changes requested, that is the firm sentence with N and the page id
     filled in. Otherwise it is `_HEAD[verdict]`;
   - `His note: {note}`, if his verdict note is non-empty;
   - a blank line;
   - `He left no marks.`, or `He left {N} marks:` (`mark:` when N is 1);
   - the items. Each is `{n}. {label}: "{quote}" (mark {id})` for range marks, or
     `{n}. General feedback about the page (mark {id})` for page comments. An item with a note
     adds `   > {note}` (one line per line of the note). Each reply adds `   - {author}: {text}`.

   It returns `("\n".join(lines) + "\n", data)`, where `data` matches the JSON-twin shape in
   Global Constraints. Each item's `state` is `row.quote_state`.

Read `comments.py` for the helper that returns open threads with replies: `list_for_artifact`,
or the `PageComments` path used by `across_pages`. Reuse it; don't write new SQL for comments.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q tests/test_reviews.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/artifacts/review.py tests/test_reviews.py
git commit -m "feat(review): one numbered message for a verdict, carrying every open mark"
```

---

### Task 3: the API and the gallery

**Files:**
- Modify: `src/artifacts/api.py`.
  - Add `class ReviewBody(BaseModel)` (no fields) next to `DecisionBody`, and two routes after
    the decisions routes (~line 1115 block).
  - Map `AlreadyDecided` on `/decisions/{id}/record` (~1356) to a 409.
- Modify: `src/artifacts/gallery.py`.
  - `_render_decisions` (~2810): a review card has a "Review" tag, a settled review renders
    without buttons, and a line under an open review says "Your marks on this page go with your
    verdict."
  - `answer_decision` (~4126): `AlreadyDecided` becomes a 409 page naming the counted verdict and
    its time.
- Test: `tests/test_reviews.py` (append)

**Interfaces:**
- Consumes: `decisions.ask_review`, `decisions.AlreadyDecided`, `decisions.VERDICTS`,
  `review.render`.
- Produces:
  - `POST /api/v1/artifacts/{artifact_id}/reviews` returns 201 with the `DecisionRow` dict
    (including `"kind": "review"`), or 404 when not yours.
  - `GET /api/v1/reviews/{review_id}` returns 200
    `{"id", "artifact_id", "verdict" (null until answered), "answered_at", "message" (null until answered), "data" (null until answered)}`,
    or 404 when it isn't yours or isn't a review.
  - The gallery's `POST /d/{id}/answer` returns 409 for a second verdict on a review.

- [ ] **Step 1: Write the failing tests**

```python
from fastapi.testclient import TestClient

from artifacts import keys
from artifacts.app import create_gallery_app

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def _auth(db, agent="seat"):
    token, _ = keys.mint(db, agent=agent)
    return {"Authorization": f"Bearer {token}"}


def test_an_agent_asks_and_reads_back_the_verdict(client, db):
    h = _auth(db)
    page = _page(db)
    asked = client.post(f"/api/v1/artifacts/{page.id}/reviews", headers=h, json={})
    assert asked.status_code == 201 and asked.json()["kind"] == "review"
    rid = asked.json()["id"]
    before = client.get(f"/api/v1/reviews/{rid}", headers=h).json()
    assert before["verdict"] is None and before["message"] is None
    client.post(f"/d/{rid}/answer", data={"letter": "c", "note": ""},
                headers=SAME_ORIGIN, follow_redirects=False)
    after = client.get(f"/api/v1/reviews/{rid}", headers=h).json()
    assert after["verdict"] == "changes_requested"
    assert after["message"].startswith("Artifacts review: CHANGES REQUESTED")
    assert after["data"]["verdict"] == "changes_requested"


def test_not_yours_and_not_a_review_look_identical(client, db):
    h, other = _auth(db), _auth(db, "other")
    page = _page(db)
    rid = client.post(f"/api/v1/artifacts/{page.id}/reviews", headers=h, json={}).json()["id"]
    assert client.get(f"/api/v1/reviews/{rid}", headers=other).status_code == 404
    q = decisions.ask(db, agent="seat", artifact_id=page.id, question="q?",
                      options=[{"letter": "a", "label": "x"}, {"letter": "b", "label": "y"}])
    assert client.get(f"/api/v1/reviews/{q.id}", headers=h).status_code == 404
    assert client.post(f"/api/v1/artifacts/{page.id}/reviews", headers=other,
                       json={}).status_code == 404


def test_a_second_verdict_from_another_device_is_409_and_names_what_counted(client, db):
    h = _auth(db)
    page = _page(db)
    rid = client.post(f"/api/v1/artifacts/{page.id}/reviews", headers=h, json={}).json()["id"]
    first = client.post(f"/d/{rid}/answer", data={"letter": "a"}, headers=SAME_ORIGIN,
                        follow_redirects=False)
    assert first.status_code == 303
    second = client.post(f"/d/{rid}/answer", data={"letter": "c"}, headers=SAME_ORIGIN,
                         follow_redirects=False)
    assert second.status_code == 409
    assert "Approved" in second.text, "he is told which verdict counted"
    assert client.get(f"/api/v1/reviews/{rid}", headers=h).json()["verdict"] == "approved"


def test_the_gallery_shows_a_review_card_and_a_settled_one_without_buttons(client, db):
    h = _auth(db)
    page = _page(db)
    rid = client.post(f"/api/v1/artifacts/{page.id}/reviews", headers=h, json={}).json()["id"]
    open_html = client.get(f"/a/{page.id}").text
    assert "Review requested" in open_html and "go with your verdict" in open_html
    assert f'action="/d/{rid}/answer"' in open_html
    client.post(f"/d/{rid}/answer", data={"letter": "b"}, headers=SAME_ORIGIN,
                follow_redirects=False)
    settled = client.get(f"/a/{page.id}").text
    assert f'action="/d/{rid}/answer"' not in settled, "one verdict counts: no buttons after"
    assert "Approved with notes" in settled
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest -q tests/test_reviews.py -k "agent_asks or look_identical or second_verdict or review_card"`
Expected: FAIL (404 on the new routes).

- [ ] **Step 3: Implement**

`api.py`:

```python
    @router.post("/artifacts/{artifact_id}/reviews", status_code=201)
    def ask_review(
        artifact_id: str,
        request: Request,
        agent: str = Depends(caller),
    ):
        """Ask Jeremy to review this page (page review step 2). A decision of
        kind 'review' with four fixed verdicts; answered in the gallery like any
        question, and read back with GET /reviews/{id}."""
        try:
            row = decisions.ask_review(
                db, agent=agent, artifact_id=artifact_id, session=session(request)
            )
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc
        return row.__dict__

    @router.get("/reviews/{review_id}")
    def read_review(review_id: str, agent: str = Depends(caller)):
        """The verdict and the one message built from it. Not yours, and not a
        review, answer exactly like a missing id."""
        row = decisions.get_owned(db, agent, review_id)
        if row is None or row.kind != "review":
            raise HTTPException(404, "no such review")
        if row.answer is None:
            return {"id": row.id, "artifact_id": row.artifact_id, "verdict": None,
                    "answered_at": None, "message": None, "data": None}
        message, data = review.render(db, review_id, agent=agent)
        return {"id": row.id, "artifact_id": row.artifact_id,
                "verdict": decisions.VERDICTS[row.answer],
                "answered_at": row.answered_at, "message": message, "data": data}
```

- `session(request)` is the existing helper the decisions POST route uses to read
  `x-artifacts-session`. Copy exactly how the existing `POST /artifacts/{id}/decisions` route
  gets it.
- If `decisions.get_owned` does not exist, add it to `decisions.py` in this task. It returns the
  `DecisionRow` for `decision_id` when its artifact is owned by `agent`, else `None`, using the
  same ownership join `close()` uses (`grep -n "def close" -A30 src/artifacts/decisions.py`).
- Add a unit test for it: owned returns the row; someone else's or a missing id returns None.

`/decisions/{id}/record`: add `except decisions.AlreadyDecided as exc:`, which raises
`HTTPException(409, {"field": "answer", "verdict": decisions.VERDICTS.get(exc.row.answer), "answered_at": exc.row.answered_at})`.
Only if `record_elsewhere` can reach the same rule. Check whether it goes through `answer()`. If
it has its own update, add the same kind/answer guard there, and a test that a relayed second
verdict on a review is a 409.

`gallery.py`, `_card` in `_render_decisions`:

```python
        is_review = getattr(row, "kind", "question") == "review"
        tag = '<span class="rtag">Review</span> ' if is_review else ""
        hint = (
            '<p class="rhint">Your marks on this page go with your verdict.</p>'
            if is_review and row.answer is None else ""
        )
        if is_review and row.answer is not None:
            label = next(o["label"] for o in row.options if o["letter"] == row.answer)
            return (
                f'<div class="ask"><p class="q">{tag}{escape(row.question)}</p>'
                f'<p class="verdict">Your verdict: <b>{escape(label)}</b>'
                f' &middot; {escape(_when(row.answered_at or ""))}</p></div>'
            )
```

Then keep the existing return, with `{tag}` before the question text and `{hint}` after
`_since(row)`. `_when` is the gallery's existing time formatter; confirm its name with
`grep -n "def _when" src/artifacts/gallery.py`. Add CSS beside `.ask`:
`.rtag{font:600 .7rem var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--accent)}`,
`.rhint{font-size:.85rem;color:var(--muted);margin:.2rem 0}` and `.verdict{margin:.3rem 0}`.

`answer_decision`: add

```python
        except decisions.AlreadyDecided as exc:
            label = next(
                (o["label"] for o in exc.row.options if o["letter"] == exc.row.answer),
                exc.row.answer,
            )
            return HTMLResponse(
                _shell(
                    "Already decided",
                    f"<p>This review already has a verdict: <b>{escape(label)}</b>, "
                    f"given {escape(_when(exc.row.answered_at or ''))}. That is the one "
                    f'that counts. <a href="/a/{escape(exc.row.artifact_id)}">Back to the '
                    "page</a></p>",
                ),
                status_code=409,
                headers=gallery_headers(),
            )
```

Check that `_shell(title, body)` exists with this shape (`grep -n "def _shell" src/artifacts/gallery.py`)
and adapt the call if it doesn't.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q tests/test_reviews.py tests/test_gallery.py tests/test_decisions*.py tests/test_page_comments.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/artifacts/api.py src/artifacts/gallery.py src/artifacts/decisions.py tests/test_reviews.py
git commit -m "feat(api): ask for a review, read the verdict back as one message, and a second verdict is refused"
```

---

### Task 4: the agent's commands, `ask-review` and `wait --review`

**Files:**
- Modify: `src/artifacts/decide.py`: the argument parser (~line 480) and dispatch (~634); a new
  `_wait_review(args, key, base) -> int` beside `_wait` (~262).
- Test: `tests/test_decide_review.py` (new). Reuse the live-server fixture pattern from
  `tests/test_client.py` (`live`, `key_file`); import them or copy the fixture code, whichever the
  repo's other CLI test files do (`grep -ln "def live" tests/`).

**Interfaces:**
- Consumes: `POST /api/v1/artifacts/{id}/reviews` and `GET /api/v1/reviews/{id}` (Task 3).
- Produces:
  - `artifacts-decide ask-review <page-id>` prints the review id on the first line of stdout
    (then the same human summary `ask` prints) and exits 0.
  - `artifacts-decide wait --review <id> [--timeout 55m] [--json] [--result-file F] [--require-approval] [--verdict-only]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_decide_review.py
"""`artifacts-decide ask-review` and `wait --review` (page review step 2)."""

import json

import pytest

from artifacts import decide, decisions, store
from tests.test_client import key_file, live  # noqa: F401 -- fixtures

PAGE = "<!doctype html><title>Plan</title><p>Rotate the keys every 90 days.</p>"


def _setup(db, key_file, capsys):
    path, _ = key_file
    page = store.publish(db, "ops-seat", "Plan", PAGE)
    assert decide.main(["--key-file", str(path), "ask-review", page.id]) == 0
    rid = capsys.readouterr().out.splitlines()[0].split()[0]
    return path, page, rid


@pytest.mark.parametrize("letter,code", [("a", 0), ("b", 4), ("c", 5), ("d", 6)])
def test_each_verdict_has_its_exit_code(live, key_file, db, capsys, letter, code):
    path, page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter=letter, by="jeremy")
    assert decide.main(["--key-file", str(path), "wait", "--review", rid,
                        "--timeout", "5s"]) == code
    assert capsys.readouterr().out.startswith("Artifacts review:")


def test_a_verdict_given_before_the_wait_is_seen_at_once(live, key_file, db, capsys):
    path, page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter="c", by="jeremy")
    import time
    t = time.monotonic()
    assert decide.main(["--key-file", str(path), "wait", "--review", rid,
                        "--timeout", "30s"]) == 5
    assert time.monotonic() - t < 10


def test_require_approval_collapses_to_zero_or_one(live, key_file, db, capsys):
    path, page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter="b", by="jeremy")
    assert decide.main(["--key-file", str(path), "wait", "--review", rid,
                        "--timeout", "5s", "--require-approval"]) == 0


def test_json_prints_the_data_on_one_line(live, key_file, db, capsys):
    path, page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter="c", by="jeremy")
    assert decide.main(["--key-file", str(path), "wait", "--review", rid,
                        "--timeout", "5s", "--json"]) == 5
    out = capsys.readouterr().out.strip()
    assert "\n" not in out and json.loads(out)["verdict"] == "changes_requested"


def test_result_file_gets_the_same_bytes_and_is_never_clobbered(live, key_file, db, capsys, tmp_path):
    path, page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter="a", by="jeremy")
    target = tmp_path / "verdict.txt"
    assert decide.main(["--key-file", str(path), "wait", "--review", rid,
                        "--timeout", "5s", "--result-file", str(target)]) == 0
    out = capsys.readouterr().out
    assert target.read_text() == out
    target.write_text("keep me")
    assert decide.main(["--key-file", str(path), "wait", "--review", rid,
                        "--timeout", "5s", "--result-file", str(target)]) == 2
    assert target.read_text() == "keep me"


def test_new_marks_before_a_verdict_wake_with_seven(live, key_file, db, capsys):
    from artifacts import comments
    path, page, rid = _setup(db, key_file, capsys)
    comments.add(db, artifact_id=page.id, author_kind="human", author="jeremy",
                 body="hm", kind="comment")
    assert decide.main(["--key-file", str(path), "wait", "--review", rid,
                        "--timeout", "5s"]) == 7


def test_verdict_only_ignores_marks_and_times_out(live, key_file, db, capsys):
    from artifacts import comments
    path, page, rid = _setup(db, key_file, capsys)
    comments.add(db, artifact_id=page.id, author_kind="human", author="jeremy",
                 body="hm", kind="comment")
    assert decide.main(["--key-file", str(path), "wait", "--review", rid,
                        "--timeout", "3s", "--verdict-only"]) == 3


def test_a_review_that_is_not_yours_is_refused(live, key_file, db, capsys):
    path, _ = key_file
    other = store.publish(db, "other", "Theirs", PAGE)
    rid = decisions.ask_review(db, agent="other", artifact_id=other.id).id
    assert decide.main(["--key-file", str(path), "wait", "--review", rid,
                        "--timeout", "3s"]) == 1
```

"New marks" means open human root comments on the page created after the review was asked
(its `created_at`). So any mark he makes after the agent asked counts until a verdict arrives,
even one made before the waiter started. The test above follows that rule: the mark is added
after `ask-review`, and the wait exits 7.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest -q tests/test_decide_review.py`
Expected: FAIL (an argparse error for `ask-review`).

- [ ] **Step 3: Implement**

- Parser: add `ask-review` with a positional `page`. Add `--review ID`, `--json`,
  `--result-file PATH`, `--require-approval` and `--verdict-only` to the existing `wait`
  subparser. `--json`, `--result-file` and `--require-approval` are errors (exit 2) without
  `--review`.
- `ask-review`: `POST {base}/artifacts/{page}/reviews` with `{}`. Print `f"{row['id']}  review requested on {page}"`,
  then `  wait: artifacts-decide wait --review {row['id']}`.
- `_wait_review`:
  1. If `--result-file` is given and exists, print `REFUSED: {path} exists; not overwriting it`
     to stderr and return 2.
  2. Loop until the deadline, starting by looking:
     - `GET /reviews/{id}`. A 404 or 401 means print `REFUSED ...` and return 1, following how
       `_request` failures are handled elsewhere in decide.py.
     - If `verdict` is set, `out = json.dumps(data) + "\n"` with `--json`, else
       `out = message`. `sys.stdout.write(out)`. Then, if a result file was given, write `out`
       to a temp file in the same directory with mode 0600, `os.link(tmp, target)`, and remove
       the temp file. If `os.link` raises `FileExistsError`, print the refusal and return 2.
       Map the verdict to its code: `approved` 0, `approved_with_notes` 4,
       `changes_requested` 5, `dismissed` 6. Apply `--require-approval`, and return.
     - Else, unless `--verdict-only`: `GET /artifacts/{artifact_id}/comments` and count the
       open human root comments created after the review's `created_at`. The review row is in
       `GET /artifacts/{artifact_id}/decisions`; read `created_at` from it once. If any exist,
       print them with `_fmt_thread` (reuse `_threads`) and return 7, or 1 with
       `--require-approval`.
     - Otherwise, sleep with the existing hold strategy (`_HOLD`, events long-poll if `_wait`
       uses one; else `time.sleep(min(5, remaining))`).
  3. On the deadline, print `nothing yet: no verdict before the timeout. Run it again.` and
     return 3.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q tests/test_decide_review.py tests/test_decide*.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/artifacts/decide.py tests/test_decide_review.py
git commit -m "feat(tools): an agent asks for a review and waits for the verdict, with an exit code per verdict"
```

---

### Task 5: tell the agents, and ship

**Files:**
- Modify: `src/artifacts/onboarding.py`, the served skill's "The loop" block. After the
  `artifacts-decide ask` line, add:
  `    artifacts-decide ask-review <page-id>                  # ask him to review the page`
  `    artifacts-decide wait --review <review-id>             # 0 approved 4 notes 5 changes 6 dismissed`
- Modify: `src/artifacts/news.py`. Add a new first entry:
  `("2026-09-29b", "artifacts-decide ask-review <page-id> asks Jeremy to review a page; wait --review <id> prints one numbered message with his verdict and marks, and exits 0 approved, 4 approved with notes, 5 changes requested, 6 dismissed, 7 new marks with no verdict yet.")`
- Test: `tests/test_tools.py` or the onboarding test that checks the loop block. Check which one
  asserts on the skill text (`grep -rn "artifacts-decide ask" tests/`).

- [ ] **Step 1: Failing test** asserting that the served skill (`GET /api/v1/skill.md`) contains
  `artifacts-decide ask-review` and `wait --review`.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Implement both edits.**
- [ ] **Step 4:** run the full suite:
  `ARTIFACTS_BROWSER=/usr/lib64/chromium-browser/headless_shell uv run pytest -q`, plus
  `uv run ruff check src tests` and `uv run ruff format --check src tests`. All pass.
- [ ] **Step 5: Commit**
  `git commit -m "docs(skill): agents learn to ask for a review and wait for the verdict"`.

Merging, pushing, deploying and the live proof are done by the controller after the final
review. The live proof, on a throwaway page:
- `ask-review`;
- Jeremy (or the controller, recording through the gallery route in the pod on a throwaway page)
  gives a verdict;
- `wait --review` exits with the matching code and prints the message;
- a second verdict gets 409.

---

## Self-review

- **Spec step-2 coverage:**
  - `decision.kind`, the fixed options and the message: Tasks 1 and 2;
  - the review bar, as the gallery card, and the 409 stale verdict: Task 3;
  - `ask-review`, `wait --review`, the codes, `--json`, `--result-file` and
    `--require-approval`: Task 4;
  - agents told: Task 5.

  Held marks, the hold/live switch, rounds, diffs, the claim/lease, presence, release, and the
  `--hook` modes are later steps.
- **Placeholders:** `render`'s body is specified as exact steps and strings, not code, because it
  must reuse the comments helper whose exact name the implementer checks first. Every string it
  emits is given verbatim.
- **Types:** `VERDICTS`, `REVIEW_OPTIONS`, `AlreadyDecided.row`, `ask_review` and
  `render -> (str, dict)` are consistent across tasks. `get_owned` is introduced in Task 3 if it
  is missing.
- **Review Focus:** items 1 and 2 are pinned in Task 2, item 3 in Tasks 1 and 3, and items 4 and
  5 in Task 4.
