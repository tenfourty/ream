"""Round 2 and later (page review step 4, homelab-ij8.108): the page opens on
what changed since the version he sent back, the card shows the round, a
Hide/Show changes link and his earlier marks with their state, and the
verdict message says the round and tags each earlier mark."""

import re

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, decisions, frame_pass, nudge, pagediff, review, store
from artifacts.app import create_gallery_app
from artifacts.content import create_content_app

PAGE = (
    "<!doctype html><title>Plan</title>"
    "<p>Rotate the keys every 90 days.</p>"
    "<p>Email the team when done.</p>"
    "<p>Keep the old runbook around.</p>"
)
ROUND2 = (
    "<!doctype html><title>Plan</title>"
    "<p>Rotate the keys every 30 days.</p>"
    "<p>Email the team when done.</p>"
    "<p>Also tell ops.</p>"
)
RANGE = {"kind": "range", "path": "/p[1]", "start": 0, "end": 6}
FRAMED = {"sec-fetch-dest": "iframe"}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(nudge, "emit", lambda **kw: None)
    pagediff._CACHE.clear()


@pytest.fixture()
def gallery(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def _content(db, page_id, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{page_id}.tenfourty.com"})
    return client


def _republish(db, page, body, agent="seat"):
    with db.connect() as conn:
        revision = conn.execute(
            "select v.revision from artifact a join version v"
            " on v.artifact_id = a.id and v.n = a.current_version where a.id = %s",
            (page.id,),
        ).fetchone()[0]
    return store.patch(db, agent, page.id, body, revision)


def _mark(db, page, quote, *, prefix=None, suffix=None, kind="comment", body="x"):
    anchor = dict(RANGE)
    if prefix is not None:
        anchor["prefix"] = prefix
    if suffix is not None:
        anchor["suffix"] = suffix
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        kind=kind,
        anchor=anchor,
        quote=quote,
        body=body,
    )


def _round_two(db, body=ROUND2):
    """Round 1 at v1 with three marks, sent back; republished as round 2."""
    page = store.publish(db, "seat", "Plan", PAGE)
    rev = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    marks = {
        "still": _mark(db, page, "Email the team when done."),
        "changed": _mark(
            db, page, "Rotate the keys every 90 days.", suffix="Email the team"
        ),
        "gone": _mark(db, page, "Keep the old runbook around.", kind="remove"),
    }
    decisions.answer(db, rev.id, letter="c", note="tighten it", by="jeremy")
    _republish(db, page, body)
    return page, rev, marks


def _last_pass(db):
    with db.connect() as conn:
        return conn.execute(
            "select version, changes_from from frame_pass order by created_at desc"
            " limit 1"
        ).fetchone()


# ---- frame passes and the content route ----


def test_a_changes_pass_serves_the_marked_up_page(db, monkeypatch):
    page, _rev, _marks = _round_two(db)
    content = _content(db, page.id, monkeypatch)
    token = frame_pass.mint(db, page.id, changes_from=1)
    html = content.get(f"/p/{token}/", headers=FRAMED).text
    assert '<ins class="ar-diff-ins">30</ins>' in html
    assert '<del class="ar-diff-del">90</del>' in html
    assert "ins.ar-diff-ins{" in html  # the styles travel with the view
    assert "window.claude" in html  # the same shim


def test_a_normal_pass_never_serves_marks(db, monkeypatch):
    page, _rev, _marks = _round_two(db)
    content = _content(db, page.id, monkeypatch)
    html = content.get(f"/p/{frame_pass.mint(db, page.id)}/", headers=FRAMED).text
    assert "ar-diff-ins" not in html and "ar-diff-del" not in html
    assert "30 days" in html


def test_the_changes_view_is_never_stored_as_a_version(db, monkeypatch):
    page, _rev, _marks = _round_two(db)
    content = _content(db, page.id, monkeypatch)
    content.get(f"/p/{frame_pass.mint(db, page.id, changes_from=1)}/", headers=FRAMED)
    with db.connect() as conn:
        bodies = [
            r[0]
            for r in conn.execute(
                "select body from version where artifact_id = %s", (page.id,)
            )
        ]
    assert len(bodies) == 2 and not any("ar-diff" in b for b in bodies)


def test_a_changes_pass_for_another_page_is_refused(db, monkeypatch):
    page, _rev, _marks = _round_two(db)
    other = store.publish(db, "seat", "Other", "<p>other</p>")
    content = _content(db, page.id, monkeypatch)
    theirs = frame_pass.mint(db, other.id, changes_from=1)
    html = content.get(f"/p/{theirs}/", headers=FRAMED).text
    assert "ar-diff" not in html and "30 days" not in html
    spent = frame_pass.spend(db, theirs, other.id)
    assert spent is not None and spent.changes_from == 1  # not burned


def test_spend_carries_changes_from(db):
    page = store.publish(db, "seat", "Plan", PAGE)
    token = frame_pass.mint(db, page.id, changes_from=3)
    spent = frame_pass.spend(db, token, page.id)
    assert spent == frame_pass.Spent(page.id, None, 3)
    plain = frame_pass.spend(db, frame_pass.mint(db, page.id), page.id)
    assert plain.changes_from is None


# ---- the gallery page view ----


def test_round_two_frames_the_changes_view_by_default(db, gallery):
    page, _rev, _marks = _round_two(db)
    html = gallery.get(f"/a/{page.id}").text
    assert _last_pass(db) == (None, 1)
    assert "Round 2" in html
    assert f'href="/a/{page.id}?changes=0"' in html and "Hide changes" in html


def test_changes_off_frames_the_plain_page(db, gallery):
    page, _rev, _marks = _round_two(db)
    html = gallery.get(f"/a/{page.id}?changes=0").text
    assert _last_pass(db) == (None, None)
    assert "Round 2" in html
    assert f'href="/a/{page.id}?changes=1"' in html and "Show changes" in html


def test_round_one_has_no_round_label_or_toggle(db, gallery):
    page = store.publish(db, "seat", "Plan", PAGE)
    decisions.ask_review(db, agent="seat", artifact_id=page.id)
    html = gallery.get(f"/a/{page.id}").text
    assert _last_pass(db) == (None, None)
    assert "Round 1" not in html and "Hide changes" not in html
    assert '<div class="carried">' not in html


def test_the_strip_labels_each_earlier_mark(db, gallery):
    page, _rev, _marks = _round_two(db)
    new = _mark(db, page, "Also tell ops.")
    html = gallery.get(f"/a/{page.id}").text
    strip = re.search(r'<div class="carried">.*?</div>', html, re.DOTALL).group(0)
    assert strip.count("<li") == 3
    assert "still on page" in strip and "text changed" in strip
    assert "addressed/removed" in strip
    assert "Also tell ops" not in strip and new.id not in strip


def test_a_resolved_earlier_mark_reads_addressed(db, gallery):
    page, _rev, marks = _round_two(db)
    comments.resolve(db, marks["still"].id, by="jeremy", agent=None)
    html = gallery.get(f"/a/{page.id}").text
    strip = re.search(r'<div class="carried">.*?</div>', html, re.DOTALL).group(0)
    assert strip.count("addressed/removed") == 2


def test_the_strip_escapes_the_quote(db, gallery):
    page = store.publish(db, "seat", "Plan", PAGE)
    rev = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    _mark(db, page, "<img src=x onerror=alert(1)>")
    decisions.answer(db, rev.id, letter="c", note="", by="jeremy")
    _republish(db, page, ROUND2)
    html = gallery.get(f"/a/{page.id}").text
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_a_fallback_reason_is_shown_on_the_card(db, gallery):
    script = "<script>" + "var a=1;" * 2000 + "</script>"
    page = store.publish(db, "seat", "Plan", PAGE + script)
    rev = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    decisions.answer(db, rev.id, letter="c", note="", by="jeremy")
    _republish(db, page, ROUND2 + script)
    html = gallery.get(f"/a/{page.id}").text
    assert "Showing the page as it now is: this page draws itself with script" in html
    assert (
        "Showing the page as it now is"
        not in gallery.get(f"/a/{page.id}?changes=0").text
    )


def test_a_mark_between_the_verdict_and_the_republish_is_carried(db, gallery):
    page = store.publish(db, "seat", "Plan", PAGE)
    rev = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    decisions.answer(db, rev.id, letter="c", note="", by="jeremy")
    between = _mark(db, page, "Email the team when done.")
    assert between.id  # no round is open, so it has no review_round
    _republish(db, page, ROUND2)
    html = gallery.get(f"/a/{page.id}").text
    strip = re.search(r'<div class="carried">.*?</div>', html, re.DOTALL).group(0)
    assert "still on page" in strip


# ---- carried state ----


def _row(db, mark_id):
    return next(
        c
        for c in comments.list_for_artifact(
            db, _page_of(db, mark_id), include_resolved=True
        )
        if c.id == mark_id
    )


def _page_of(db, mark_id):
    with db.connect() as conn:
        return conn.execute(
            "select artifact_id from comment where id = %s", (mark_id,)
        ).fetchone()[0]


def test_emphasis_inside_a_quote_stays_still_on_page(db):
    page = store.publish(db, "seat", "Plan", PAGE)
    mark = _mark(db, page, "Rotate the keys every 90 days.")
    body = PAGE.replace(
        "Rotate the keys every 90 days.",
        "Rotate the <em>keys</em> every <b>90</b> days.",
    )
    assert comments.carried_state(_row(db, mark.id), body) == "still on page"


def test_carried_states(db):
    page = store.publish(db, "seat", "Plan", PAGE)
    still = _mark(db, page, "Email the team when done.")
    changed = _mark(db, page, "Rotate the keys every 90 days.", suffix="Email the")
    gone = _mark(db, page, "Keep the old runbook around.")
    general = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        anchor={"kind": "page"},
        body="overall",
    )
    assert comments.carried_state(_row(db, still.id), ROUND2) == "still on page"
    assert comments.carried_state(_row(db, changed.id), ROUND2) == "text changed"
    assert comments.carried_state(_row(db, gone.id), ROUND2) == "addressed/removed"
    assert comments.carried_state(_row(db, general.id), ROUND2) == "still on page"
    comments.resolve(db, still.id, by="jeremy", agent=None)
    assert comments.carried_state(_row(db, still.id), ROUND2) == "addressed/removed"


# ---- the message ----


def test_round_one_first_line_is_unchanged(db):
    page = store.publish(db, "seat", "Plan", PAGE)
    rev = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    _mark(db, page, "Email the team when done.")
    decisions.answer(db, rev.id, letter="c", note="", by="jeremy")
    message, data = review.render(db, rev.id, agent="seat")
    assert message.splitlines()[0] == (
        f'Artifacts review: CHANGES REQUESTED — "Plan" ({page.id}, v1)'
    )
    assert "[still on page]" not in message
    assert (data["round"], data["version"], data["base_version"]) == (1, 1, None)
    assert [i["state"] for i in data["items"]] == ["new"]


def test_round_two_says_its_round_and_tags_earlier_marks(db):
    page, rev, marks = _round_two(db)
    new = _mark(db, page, "Also tell ops.")
    decisions.answer(db, rev.id, letter="c", note="again", by="jeremy", round=2)
    message, data = review.render(db, rev.id, agent="seat")
    lines = message.splitlines()
    assert lines[0] == (
        f'Artifacts review: CHANGES REQUESTED — "Plan" ({page.id}, v2, round 2)'
    )
    assert f"(mark {marks['still'].id}) [still on page]" in message
    assert f"(mark {marks['changed'].id}) [text changed]" in message
    assert f"(mark {marks['gone'].id}) [addressed/removed]" in message
    assert re.search(rf"\(mark {new.id}\)$", message, re.MULTILINE)
    assert (data["round"], data["version"], data["base_version"]) == (2, 2, 1)
    states = {i["id"]: i["state"] for i in data["items"]}
    assert states == {
        marks["still"].id: "still on page",
        marks["changed"].id: "text changed",
        marks["gone"].id: "addressed/removed",
        new.id: "new",
    }


def test_round_two_carries_a_resolved_earlier_mark(db):
    _page, rev, marks = _round_two(db)
    comments.resolve(db, marks["gone"].id, by="jeremy", agent=None)
    decisions.answer(db, rev.id, letter="a", note="", by="jeremy", round=2)
    message, data = review.render(db, rev.id, agent="seat")
    assert f"(mark {marks['gone'].id}) [addressed/removed]" in message
    assert len(data["items"]) == 3
