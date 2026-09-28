# Page review, step 1: marks on every page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On any page, Jeremy can select text and choose *Comment*, *Remove this* or *Looks
good*. The marks are painted on the page where their text is, and agents read each mark's kind.

**Architecture:** A mark is a `comment` row with a new `kind` column. The selection anchor gains
optional `prefix`/`suffix` context. The gallery hands the open marks to the page's shim, which
places each one by verify-then-search and paints it with the CSS Custom Highlight API, without
touching the agent's DOM. It reports marks it could not place. No new tables, and no new
routes except fields on existing ones.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres, and inline JavaScript strings in
`content.py` / `gallery.py`. Tests use pytest against a real Postgres (`PG_TEST_DSN`).

**Spec:** `docs/agent/specs/2026-09-28-page-review-design.md` (sections "What Jeremy sees",
"Data", "Anchoring and painting"). Research: `docs/agent/research/2026-09-28-plannotator-study.md`.

## Global Constraints

- **Ideas only from plannotator; no copied code** (Jeremy, 28 Sep 2026).
- A mark is never guessed onto text: if the stored position and the text search both fail, or
  the search is ambiguous, the mark is **unplaced**, never painted somewhere else.
- The shim never changes the agent's DOM. It paints only through `CSS.highlights`. The fallback
  is to paint nothing and report the mark unplaced.
- The shim accepts messages only from the gallery origin, as today, and never sends note text
  into the page. It receives quotes and anchors only.
- Kinds are exactly `comment`, `remove` and `looks_good`. A reply is always `comment`.
- `remove` and `looks_good` need a `range` anchor and a non-empty quote. Their body (note) may
  be empty.
- Every refusal names its field and never echoes caller input (`validate_anchor`'s rule).
- Commit messages are conventional, with no Co-Authored-By line (the repo guard enforces this).
  Run `uv run ruff format` and `uv run ruff check src tests` before each commit.

---

## File map

| File | Change |
|---|---|
| `src/artifacts/schema.sql` | `comment.kind` column |
| `src/artifacts/comments.py` | `KINDS`, `InvalidKind`, `CommentRow.kind`, `_COLUMNS`, `add(kind=)`, `validate_anchor` prefix/suffix, `marks_for_page()` |
| `src/artifacts/api.py` | `CommentBody.kind`, and the 422 for `InvalidKind` |
| `src/artifacts/gallery.py` | `/c/{id}` takes `kind`; body optional for marks; three buttons in `_COMMENTS_JS`; the marks JSON on the page; the kind label in the thread; the "unplaced" note |
| `src/artifacts/content.py` | the shim sends `prefix`/`suffix`, and gains a `artifact-marks` listener that places and paints |
| `src/artifacts/decide.py` | `_fmt_thread` prints the kind |
| `src/artifacts/news.py` | one entry |
| `tests/test_comment_kinds.py` (new) | model, API and gallery tests |
| `tests/test_shim_marks.py` (new) | shim string-level tests, and the headless-browser test when a browser exists |

---

### Task 1: `comment.kind` in the model and storage

**Files:**
- Modify: `src/artifacts/schema.sql` (after the `comment` table and its indexes, around line 108)
- Modify: `src/artifacts/comments.py:36-90, 181-330`
- Test: `tests/test_comment_kinds.py`

**Interfaces:**
- Produces:
  - `comments.KINDS: tuple[str, ...] = ("comment", "remove", "looks_good")`
  - `class comments.InvalidKind(Exception)`, with `.field == "kind"`
  - `CommentRow.kind: str`
  - `comments.add(..., kind: str = "comment")`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_comment_kinds.py
"""Marks on every page: a comment can be a remark, a 'remove this' or a 'looks
good' (page review step 1, homelab-ij8.108)."""

import pytest

from artifacts import comments, events, store

PAGE = "<!doctype html><title>t</title><p>Rotate the keys every 90 days.</p>"
RANGE = {"kind": "range", "path": "body>p:nth-of-type(1)", "start": 0, "end": 29}


def _page(db):
    return store.publish(db, "seat", "Board", PAGE)


def test_a_plain_comment_is_kind_comment(db):
    page = _page(db)
    row = comments.add(db, artifact_id=page.id, author_kind="human",
                       author="jeremy", body="why 90?")
    assert row.kind == "comment"


@pytest.mark.parametrize("kind", ["remove", "looks_good"])
def test_a_mark_needs_no_note_but_needs_a_quote(db, kind):
    page = _page(db)
    row = comments.add(db, artifact_id=page.id, author_kind="human",
                       author="jeremy", body="", kind=kind,
                       quote="Rotate the keys every 90 days.", anchor=RANGE)
    assert row.kind == kind and row.body == ""
    with pytest.raises(comments.InvalidKind):
        comments.add(db, artifact_id=page.id, author_kind="human",
                     author="jeremy", body="", kind=kind)  # page anchor, no quote


def test_an_unknown_kind_is_refused_without_echoing_it(db):
    page = _page(db)
    with pytest.raises(comments.InvalidKind) as err:
        comments.add(db, artifact_id=page.id, author_kind="human",
                     author="jeremy", body="x", kind="<script>")
    assert "<script>" not in str(err.value)


def test_a_reply_is_always_a_comment(db):
    page = _page(db)
    root = comments.add(db, artifact_id=page.id, author_kind="human",
                        author="jeremy", body="", kind="remove",
                        quote="Rotate the keys every 90 days.", anchor=RANGE)
    reply = comments.add(db, artifact_id=page.id, author_kind="agent",
                         author="seat", body="done", thread_root=root.id,
                         kind="looks_good")
    assert reply.kind == "comment"


def test_a_mark_with_no_note_still_says_something_to_the_agent(db):
    page = _page(db)
    comments.add(db, artifact_id=page.id, author_kind="human", author="jeremy",
                 body="", kind="remove", quote="Rotate the keys every 90 days.",
                 anchor=RANGE)
    [event] = events.list_for_agent(db, "seat")
    assert event.summary.startswith('Remove this: "Rotate the keys')
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_comment_kinds.py`
Expected: FAIL with `TypeError: add() got an unexpected keyword argument 'kind'`.

- [ ] **Step 3: Implement**

In `schema.sql`, after `create index if not exists comment_thread_idx ...`:

```sql
-- What kind of mark a comment is (page review, homelab-ij8.108, 28 Sep 2026):
-- a remark, "remove this", or "looks good". A reply is always 'comment'.
alter table comment add column if not exists kind text not null default 'comment'
    check (kind in ('comment', 'remove', 'looks_good'));
```

In `comments.py`:
- Add `KINDS = ("comment", "remove", "looks_good")` beside `MAX_QUOTE_BYTES`.
- Add `class InvalidKind(Exception)` beside `InvalidAnchor`, with the same shape:
  `super().__init__("kind"); self.field = "kind"`.
- Append `, comment.kind` to the end of the `_COLUMNS` string, so it is column index 12.
- Add `kind: str = "comment"` to `CommentRow` **before** `quote_state` (both defaulted), and
  `kind=row[12]` in `_row_to_comment`.
- In `add()`, add the parameter `kind: str = "comment"`. Then, in the "cheapest first" block
  right after the `body` size check:

```python
    if kind not in KINDS:
        raise InvalidKind()
    if is_reply:
        kind = "comment"
```

- After `effective_anchor = validate_anchor(anchor)` in the non-reply branch:

```python
        if kind != "comment" and (
            effective_anchor.get("kind") != "range" or not quote.strip()
        ):
            raise InvalidKind()
```

- Add `kind` to the INSERT column list and values.
- In the event, replace `summary=body` and `note=body` with `summary=said` and `note=said`,
  where, just before `with conn.transaction():`:

```python
        label = {"remove": "Remove this", "looks_good": "Looks good"}.get(kind)
        said = body
        if label:
            short = " ".join(effective_quote.split())[:120]
            said = f'{label}: "{short}"' + (f" — {body}" if body.strip() else "")
```

  Pass `summary=said` to `nudge.emit` as well.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest -q tests/test_comment_kinds.py tests/test_comments.py tests/test_comment_nudge.py`
Expected: PASS. The existing suites stay green because every default is `comment`.

- [ ] **Step 5: Commit**

```bash
git add src/artifacts/schema.sql src/artifacts/comments.py tests/test_comment_kinds.py
git commit -m "feat(comments): a comment can be a 'remove this' or a 'looks good' mark"
```

---

### Task 2: prefix and suffix on a range anchor

**Files:**
- Modify: `src/artifacts/comments.py` (`validate_anchor`, the `range` branch)
- Test: `tests/test_comment_kinds.py` (append)

**Interfaces:**
- Produces: a `range` anchor may carry `prefix` and `suffix` (each a `str`, at most
  `ANCHOR_CONTEXT_MAX = 64` characters). They are stored as given, and absent keys stay
  absent.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_range_anchor_may_carry_context_either_side():
    got = comments.validate_anchor({**RANGE, "prefix": "the keys ", "suffix": " and"})
    assert got == {**RANGE, "prefix": "the keys ", "suffix": " and"}
    assert comments.validate_anchor(RANGE) == RANGE, "context stays optional"


@pytest.mark.parametrize("bad", [
    {"prefix": 5}, {"suffix": None}, {"prefix": "x" * 65}, {"extra": "no"},
])
def test_context_is_validated_like_the_rest_of_the_anchor(bad):
    with pytest.raises(comments.InvalidAnchor):
        comments.validate_anchor({**RANGE, **bad})
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest -q tests/test_comment_kinds.py -k context`
Expected: FAIL. The first test raises `InvalidAnchor`, because of the exact-keys check.

- [ ] **Step 3: Implement.** Replace the `range` branch:

```python
ANCHOR_CONTEXT_MAX = 64

    if kind == "range":
        keys = set(anchor.keys())
        if not ({"kind", "path", "start", "end"} <= keys
                <= {"kind", "path", "start", "end", "prefix", "suffix"}):
            raise InvalidAnchor()
        path, start, end = anchor["path"], anchor["start"], anchor["end"]
        if not isinstance(path, str) or len(path) > 512:
            raise InvalidAnchor()
        if not (_is_int(start) and _is_int(end)):
            raise InvalidAnchor()
        if not (0 <= start < end):
            raise InvalidAnchor()
        out = {"kind": "range", "path": path, "start": start, "end": end}
        # Text either side of the quote, so a quote that appears twice on the
        # page is placed on the right one (the text-quote-selector idea; see
        # the plannotator study, section A).
        for side in ("prefix", "suffix"):
            if side in anchor:
                value = anchor[side]
                if not isinstance(value, str) or len(value) > ANCHOR_CONTEXT_MAX:
                    raise InvalidAnchor()
                out[side] = value
        return out
```

- [ ] **Step 4: Run all the comment tests**

Run: `uv run pytest -q tests/test_comment_kinds.py tests/test_comments.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/artifacts/comments.py tests/test_comment_kinds.py
git commit -m "feat(comments): a range anchor may carry the text either side of the quote"
```

---

### Task 3: kinds through the agent API and the gallery form

**Files:**
- Modify: `src/artifacts/api.py:154-158` (`CommentBody`) and `create_comment` (around
  line 1011) to pass `kind=payload.kind` and map `comments.InvalidKind` to 422
  `{"field": "kind", "detail": "kind must be comment, remove or looks_good, with a quoted range for the last two"}`.
- Modify: `src/artifacts/gallery.py` `add_comment` (around line 4072): `body: str = Form("")`,
  `kind: str = Form("comment")`, pass `kind=kind`, and map `InvalidKind` to 422. Refuse an
  empty body only when `kind == "comment"`: `if kind == "comment" and not body.strip(): raise HTTPException(422, "a comment needs some words")`.
- Test: `tests/test_comment_kinds.py` (append)

**Interfaces:**
- Consumes: `comments.add(kind=)` and `comments.InvalidKind` from Task 1.
- Produces: `POST /api/v1/artifacts/{id}/comments` accepts `"kind"`, and
  `GET /api/v1/artifacts/{id}/comments` rows carry `"kind"` (automatically, via
  `CommentRow.__dict__`). `POST /c/{id}` accepts the form field `kind`.

- [ ] **Step 1: Write the failing tests**

```python
from fastapi.testclient import TestClient

from artifacts import keys
from artifacts.app import create_gallery_app

SAME_ORIGIN = {"sec-fetch-site": "same-origin", "origin": "https://artifacts.tenfourty.site",
               "host": "artifacts.tenfourty.site"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def test_the_gallery_form_takes_a_mark_with_no_note(client, db):
    page = _page(db)
    r = client.post(f"/c/{page.id}", data={
        "kind": "remove", "body": "", "quote": "Rotate the keys every 90 days.",
        "anchor": '{"kind":"range","path":"body>p:nth-of-type(1)","start":0,"end":29}',
    }, headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    token, _ = keys.mint(db, agent="seat")
    rows = client.get(f"/api/v1/artifacts/{page.id}/comments",
                      headers={"Authorization": f"Bearer {token}"}).json()
    assert [row["kind"] for row in rows] == ["remove"]


def test_an_empty_plain_comment_is_still_refused(client, db):
    page = _page(db)
    r = client.post(f"/c/{page.id}", data={"body": "  "}, headers=SAME_ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 422


def test_an_agent_can_mark_its_own_page_and_a_bad_kind_names_the_field(client, db):
    token, _ = keys.mint(db, agent="seat")
    page = _page(db)
    h = {"Authorization": f"Bearer {token}"}
    ok = client.post(f"/api/v1/artifacts/{page.id}/comments", headers=h, json={
        "kind": "looks_good", "body": "", "quote": "Rotate the keys every 90 days.",
        "anchor": RANGE})
    assert ok.status_code == 201 and ok.json()["kind"] == "looks_good"
    bad = client.post(f"/api/v1/artifacts/{page.id}/comments", headers=h,
                      json={"kind": "nope", "body": "x"})
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "kind"
```

Before running, check how the existing gallery tests build their same-origin headers
(`grep -n "SAME_ORIGIN =" tests/*.py`), and use that constant's exact shape if it differs from
the one above.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest -q tests/test_comment_kinds.py -k "form or agent_can_mark or empty_plain"`
Expected: FAIL. `kind` is ignored, and the empty body is either accepted, or refused as a
missing field.

- [ ] **Step 3: Implement** the three changes listed under **Files**.

- [ ] **Step 4: Run**

Run: `uv run pytest -q tests/test_comment_kinds.py tests/test_comments.py tests/test_page_comments.py tests/test_comment_loop.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/artifacts/api.py src/artifacts/gallery.py tests/test_comment_kinds.py
git commit -m "feat(comments): agents and the gallery can make remove-this and looks-good marks"
```

---

### Task 4: three actions on a selection, and the kind shown in the thread

**Files:**
- Modify: `src/artifacts/content.py` (the selection block of the slot shim, around
  lines 160-178)
- Modify: `src/artifacts/gallery.py` `_COMMENTS_JS` (lines 1683-1712), and the thread's quote
  renderer (around line 2288)
- Test: `tests/test_shim_marks.py` (new)

**Interfaces:**
- Consumes: the `kind` form field (Task 3), and `prefix`/`suffix` validation (Task 2).
- Produces: `artifact-selection` messages whose `anchor` carries `prefix`/`suffix` (32
  characters each).

- [ ] **Step 1: Write the failing tests** (at the string level; the behaviour is proved in
  Task 6)

```python
# tests/test_shim_marks.py
from artifacts import content, gallery


def test_the_selection_carries_context_either_side():
    shim = content.slot_shim([]) if hasattr(content, "slot_shim") else ""
    source = shim or open(content.__file__).read()
    assert "prefix:" in source and "suffix:" in source


def test_the_gallery_offers_three_actions_on_a_selection():
    js = gallery._COMMENTS_JS
    for label in ("Comment", "Remove this", "Looks good"):
        assert f'"{label}"' in js
    assert 'field("kind"' in js
```

Before writing this, read `content.py` to find the function that builds the slot shim (it
begins `"<script>try{(function(){"` and contains `artifact-selection`), and call it by its real
name with a real argument, instead of the `hasattr` fallback above.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest -q tests/test_shim_marks.py`
Expected: FAIL.

- [ ] **Step 3: Implement**

In the shim's `sel()`, after `var a=...,z=...;`, take 32 characters either side from the
element's own text. `a` and `z` are already offsets into `n`'s text, so `n.textContent` is the
right string:

```javascript
var all=n.textContent||"",prefix=all.slice(Math.max(0,a-32),a),suffix=all.slice(z,z+32);
```

and send `anchor:{kind:"range",path:path,start:a,end:z,prefix:prefix,suffix:suffix}`.
Offsets `a`/`z` are already offsets into the element's text, so `n.textContent` is the right
string.

In `_COMMENTS_JS`, replace the single button with three buttons in a small bar:

```javascript
var bar=document.createElement("div");bar.className="selbar";bar.hidden=true;
[["comment","Comment"],["remove","Remove this"],["looks_good","Looks good"]].forEach(function(k){
  var b=document.createElement("button");b.type="button";b.textContent=k[1];b.dataset.kind=k[0];
  bar.appendChild(b)});
document.body.appendChild(bar);
```

- Position `bar` exactly where `btn` was positioned, and hide and show `bar` instead of `btn`.
- In one click handler on `bar` (`e.target.closest("button")`), set
  `field("kind",b.dataset.kind)` along with the existing `quote`/`anchor` fields.
- For `comment`, keep today's flow: the quote block, focus the body, placeholder
  "Comment on the selected text".
- For `remove`/`looks_good`, set the placeholder to "Add a note (optional)", then call
  `form.requestSubmit()` if the body is empty, so one tap makes the mark.
- Add CSS for `.selbar` next to `.selbtn` (find it with `grep -n "\.selbtn" src/artifacts/gallery.py`),
  using the same look as `.selbtn`, laid out as a row with gaps.

In the thread quote renderer (`gallery.py` around line 2288), prefix the blockquote with a
label for non-comment kinds:

```python
        label = {"remove": "Remove this", "looks_good": "Looks good"}.get(
            getattr(root, "kind", "comment"), ""
        )
        tag = f'<span class="ckind ckind-{escape(root.kind)}">{label}</span> ' if label else ""
        return f'<blockquote class="cquote">{tag}{escape(root.quote)}{note}</blockquote>'
```

Add `.ckind{font:600 .75rem var(--mono);text-transform:uppercase}`,
`.ckind-remove{color:var(--bad, #c0392b)}` and `.ckind-looks_good{color:var(--ok, #2e9d57)}`
next to `.cquote`.

- [ ] **Step 4: Run**

Run: `uv run pytest -q tests/test_shim_marks.py tests/test_page_comments.py tests/test_content*.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/artifacts/content.py src/artifacts/gallery.py tests/test_shim_marks.py
git commit -m "feat(gallery): a selection offers Comment, Remove this and Looks good"
```

---

### Task 5: marks painted on the page, and unplaced marks reported

**Files:**
- Modify: `src/artifacts/comments.py`: add `marks_for_page(db, artifact_id) -> list[dict]`
- Modify: `src/artifacts/gallery.py`: embed the marks, have `_COMMENTS_JS` post them into the
  frame and show "N marks could not be placed"
- Modify: `src/artifacts/content.py`: the shim's `artifact-marks` listener
- Test: `tests/test_shim_marks.py` (append), `tests/test_comment_kinds.py` (append)

**Interfaces:**
- Consumes: `CommentRow.kind`, and anchors with prefix/suffix.
- Produces:
  - `comments.marks_for_page(db, artifact_id)` returns
    `[{"id": str, "kind": str, "quote": str, "anchor": dict}]` for **open root** comments with
    a `range` anchor, oldest first. Never the note text.
  - The message `{type:"artifact-marks", marks:[...]}` goes gallery → frame.
  - The reply `{type:"artifact-marks-placed", placed:[id], unplaced:[id]}` goes frame →
    gallery.

- [ ] **Step 1: Write the failing tests**

```python
def test_marks_for_page_carries_no_note_text(db):
    page = _page(db)
    comments.add(db, artifact_id=page.id, author_kind="human", author="jeremy",
                 body="secret thought", kind="comment",
                 quote="Rotate the keys every 90 days.", anchor=RANGE)
    comments.add(db, artifact_id=page.id, author_kind="human", author="jeremy",
                 body="page-level", kind="comment")
    marks = comments.marks_for_page(db, page.id)
    assert [m["kind"] for m in marks] == ["comment"], "range-anchored roots only"
    assert "secret thought" not in str(marks)
```

and in `tests/test_shim_marks.py`:

```python
def test_the_shim_paints_through_css_highlights_and_never_the_dom():
    source = open(content.__file__).read()
    assert "artifact-marks" in source and "CSS.highlights" in source
    assert "surroundContents" not in source and "insertAdjacentHTML" not in source
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest -q tests/test_shim_marks.py tests/test_comment_kinds.py -k "marks or paints"`
Expected: FAIL (`AttributeError: marks_for_page`).

- [ ] **Step 3: Implement**

`comments.marks_for_page`:

```python
def marks_for_page(db: Database, artifact_id: str) -> list[dict]:
    """The open marks the page shim paints: quote and anchor only, NEVER the
    note -- the page is agent HTML on its own origin and must not read what
    Jeremy wrote about it (page review design, Security)."""
    with db.connect() as conn:
        rows = conn.execute(
            "select id, kind, quote, anchor from comment"
            " where artifact_id = %s and thread_root is null"
            " and resolved_at is null and anchor->>'kind' = 'range'"
            " order by created_at",
            (artifact_id,),
        ).fetchall()
    return [{"id": r[0], "kind": r[1], "quote": r[2], "anchor": r[3]} for r in rows]
```

Gallery page (the view that renders the frame and loads `/comments.js`): emit
`<script type="application/json" id="artifact-marks">{_script_json(marks)}</script>` beside the
comments form, with `marks = comments.marks_for_page(db, artifact_id)`. Reuse
`content._script_json`, or a local copy with the same escaping. In `_COMMENTS_JS`:

```javascript
var mk=document.getElementById("artifact-marks"),marks=[];
try{marks=JSON.parse(mk?mk.textContent:"[]")}catch(e){marks=[]}
function sendMarks(){if(marks.length)f.contentWindow.postMessage({type:"artifact-marks",marks:marks},o)}
```

- Call `sendMarks()` when the frame reports `artifact-state`: listen for that message type in
  the existing handler, and send once per frame load (`var sent=false`, reset on `f`'s `load`
  event).
- On `artifact-marks-placed` with a non-empty `unplaced` array, show a line at the top of the
  comments section: "N marks could not be placed on this version of the page."

Shim listener (`content.py`, inside the same IIFE, after the slot-sizes listener). It uses the
same origin check, and every field is type-checked:

```javascript
function tight(s){return s.replace(/\s+/g,"").toLowerCase()}
function rangeAt(el,a,z){var w=document.createTreeWalker(el,4),t,o=0,r=document.createRange(),s=0;
  while((t=w.nextNode())){var l=t.nodeValue.length;
    if(!s&&o+l>=a){r.setStart(t,a-o);s=1}
    if(s&&o+l>=z){r.setEnd(t,z-o);return r}o+=l}return null}
function search(m){var body=document.body;if(!body)return null;
  var all=body.textContent||"",q=m.quote,hits=[],i=all.indexOf(q);
  while(i>=0&&hits.length<50){hits.push(i);i=all.indexOf(q,i+1)}
  if(!hits.length)return null;
  var pre=m.anchor.prefix||"",suf=m.anchor.suffix||"",best=-1,bs=-1,tie=false;
  hits.forEach(function(h){var sc=0;
    if(pre&&all.slice(Math.max(0,h-pre.length),h)===pre)sc+=2;
    if(suf&&all.slice(h+q.length,h+q.length+suf.length)===suf)sc+=2;
    if(sc>bs){bs=sc;best=h;tie=false}else if(sc===bs)tie=true});
  if(tie&&hits.length>1)return null;   // ambiguous: never guess
  return rangeAt(body,best,best+q.length)}
function place(m){var el=null;try{el=document.querySelector(m.anchor.path)}catch(e){}
  if(el){var r=rangeAt(el,m.anchor.start,m.anchor.end);
    if(r&&tight(String(r))===tight(m.quote))return r}
  return search(m)}
addEventListener("message",function(e){if(e.origin!==ORIGIN)return;var d=e.data;
  if(!d||d.type!=="artifact-marks"||!Array.isArray(d.marks))return;
  if(!(window.CSS&&CSS.highlights&&window.Highlight))return;   // no API: paint nothing
  var groups={comment:new Highlight(),remove:new Highlight(),looks_good:new Highlight()},
      placed=[],unplaced=[];
  d.marks.slice(0,200).forEach(function(m){
    if(!m||typeof m.id!=="string"||typeof m.quote!=="string"||!m.anchor||typeof m.anchor.path!=="string")return;
    var g=groups[m.kind]||groups.comment,r=null;try{r=place(m)}catch(x){}
    if(r){g.add(r);placed.push(m.id)}else unplaced.push(m.id)});
  CSS.highlights.set("ar-comment",groups.comment);CSS.highlights.set("ar-remove",groups.remove);
  CSS.highlights.set("ar-good",groups.looks_good);
  if(!document.getElementById("ar-mark-style")){var st=document.createElement("style");
    st.id="ar-mark-style";st.textContent="::highlight(ar-comment){background:rgba(217,164,0,.28)}"
    +"::highlight(ar-remove){background:rgba(192,57,43,.22);text-decoration:line-through}"
    +"::highlight(ar-good){background:rgba(46,157,87,.22)}";document.head.appendChild(st)}
  parent.postMessage({type:"artifact-marks-placed",placed:placed,unplaced:unplaced},ORIGIN)});
```

`ORIGIN` is the shim's existing `origin` Python-interpolated literal: write it as
`f"if(e.origin!=={origin})return;"`, exactly as the slot-sizes listener does. Wrap the
registration in the shim's existing `try`. The appended `<style>` element is the one DOM
addition, and it is a stylesheet, not content, as the constraint allows. Note it in a comment.

- [ ] **Step 4: Run**

Run: `uv run pytest -q tests/test_shim_marks.py tests/test_comment_kinds.py tests/test_page_comments.py tests/test_content*.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/artifacts/comments.py src/artifacts/gallery.py src/artifacts/content.py tests/
git commit -m "feat(gallery): marks are painted on the page where their text is, and unplaced marks are reported"
```

---

### Task 6: prove the shim in a real browser

**Files:**
- Test: `tests/test_shim_marks.py` (append; skipped when no headless browser, like
  `tests/test_thumbs.py`'s `needs_browser`)

**Interfaces:**
- Consumes: the shim from Tasks 4 and 5, and `thumbs.browser_path()`.

- [ ] **Step 1: Write the browser tests**

Build a page with the shim injected (use the same function `content.py` uses to serve a page,
found in Task 4), plus a test harness script appended **for the test only**. The harness:
1. posts an `artifact-marks` message to its own window with a faked origin check. Simplest: run
   the page on `file://`, and have the harness call the listener's placement function directly.
   To allow that, expose the shim's `place` as `window.__arPlace` **only when**
   `location.protocol==="file:"`.
2. writes the results into `document.title`.

Then run chromium with `--headless --dump-dom` and assert on the title. Cases:
1. A quote that appears twice, with prefix/suffix pointing at the second: placed on the
   second.
2. The same quote twice with no context: unplaced.
3. A quote spanning two `<p>` elements: placed (whitespace-free compare).
4. A stored path now pointing at other text, with the quote elsewhere: placed at the quote, not
   at the path.
5. A quote not on the page: unplaced.

```python
needs_browser = pytest.mark.skipif(not thumbs.available(), reason="no headless browser")


@needs_browser
def test_placement_rules_in_a_real_browser(tmp_path):
    html = content_page_with_shim(_HARNESS_PAGE)  # the real serving function
    path = tmp_path / "p.html"
    path.write_text(html)
    out = subprocess.run([thumbs.browser_path(), "--headless", "--no-sandbox",
                          "--disable-gpu", "--dump-dom", path.as_uri()],
                         capture_output=True, text=True, timeout=30).stdout
    title = re.search(r"<title>(.*?)</title>", out).group(1)
    assert title == "second|unplaced|placed|followed|unplaced"
```

Write `_HARNESS_PAGE` with the five cases and a harness script. It calls `window.__arPlace`
for each and sets `document.title` to the results joined by `|`: `second` means placed and the
range's start offset lies in the second occurrence; `followed` means placed at the quote rather
than the path.

- [ ] **Step 2: Run**

Run: `uv run pytest -q tests/test_shim_marks.py -k real_browser`
Expected: PASS on baba (chromium present). It is SKIPPED in CI without a browser; say so in the
test's docstring.

- [ ] **Step 3: Commit**

```bash
git add src/artifacts/content.py tests/test_shim_marks.py
git commit -m "test(shim): the mark placement rules, proved in a real browser"
```

---

### Task 7: the agent's tools show the kind, and ship

**Files:**
- Modify: `src/artifacts/decide.py:212-248` (`_threads`, `_fmt_thread`)
- Modify: `src/artifacts/news.py` (a new first entry)
- Test: `tests/test_comment_kinds.py` (append)

- [ ] **Step 1: Write the failing test**

```python
from artifacts import decide


def test_the_agent_sees_what_kind_of_mark_it_was():
    thread = decide._threads([{
        "id": "c1", "artifact_id": "p1", "quote": "Rotate the keys",
        "kind": "remove", "author_kind": "human", "author": "jeremy",
        "body": "", "created_at": "2026-09-28T10:00:00Z", "thread_root": None}])[0]
    assert '  REMOVE THIS: "Rotate the keys"' in decide._fmt_thread(thread)
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest -q tests/test_comment_kinds.py -k kind_of_mark`
Expected: FAIL (it prints `on: "…"`).

- [ ] **Step 3: Implement**

In `_threads`, set `thread["kind"] = first.get("kind") or "comment"`. In `_fmt_thread`, replace
the `on:` line:

```python
    label = {"remove": "REMOVE THIS", "looks_good": "LOOKS GOOD"}.get(thread.get("kind"), "on")
    if thread["quote"]:
        lines.append(f'  {label}: "{thread["quote"]}"')
```

Add a news entry, `("2026-09-29a", "Jeremy can now mark text on any page as 'Remove this' or 'Looks good' as well as comment; artifacts-decide comments shows which, and comment rows carry a kind.")`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q` and `uv run ruff format --check src tests` and `uv run ruff check src tests`
Expected: all pass.

- [ ] **Step 5: Commit, push, prove live**

```bash
git add src/artifacts/decide.py src/artifacts/news.py tests/test_comment_kinds.py
git commit -m "feat(tools): the agent sees whether a mark said remove this or looks good"
git fetch && git rebase origin/main && PUSH_COUNT=<n> git push origin main
```

Wait for the image on the Deployment (see `docs/agent/handoff-*` for the waiter). Then prove it
live **on a throwaway page**:
1. an agent posts a `looks_good` mark through the API, and `artifacts-decide comments` shows
   `LOOKS GOOD:`;
2. `--preview` of the gallery view is not possible (it is behind Authelia), so ask Jeremy to
   select text on the throwaway page and try all three buttons, then confirm the paint in his
   browser.

Record the evidence on homelab-ij8.108 and bin the throwaway page.

---

## Self-review

- **Spec coverage (step 1 only):**
  - three actions on a selection (T4);
  - a page comment in the panel, which already exists;
  - `comment.kind` (T1);
  - prefix/suffix (T2, T4);
  - verify → search → unplaced (T5, proved in T6);
  - Highlight-API painting (T5);
  - the unplaced report (T5);
  - no note text into the page (T5 test);
  - agent reads the kind (T3, T7).

  `quote_state: moved`, held marks, verdicts, rounds and diffs belong to later steps' plans.
- **Placeholders:** none. Two steps tell the engineer to look up an exact existing name first
  (the shim-building function in T4, and the same-origin header constant in T3), because
  guessing it would be wrong.
- **Types:** `KINDS`, `InvalidKind`, `CommentRow.kind`, `marks_for_page` and
  `artifact-marks` / `artifact-marks-placed` are used consistently across tasks.
