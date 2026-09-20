"""Use the gallery the way a person does, and report anything that misbehaves.

Not a test of a behaviour -- a harness for FINDING them. Fourteen journeys that
follow every link, submit every form by its own method, and check that each
action both lands somewhere sensible and actually did the thing.

It earned its place on 21 Sep 2026: it found that archiving or deleting a page
holding an unanswered question made the question vanish from the gallery while
leaving it open in the database, with the agent that asked still waiting. No
amount of reading the code had shown that, and no screenshot could.

Two lessons from writing it, both of which cost a round of false findings:
  - a sweep that clicks every form will click Delete, and then everything after
    it 404s. Destructive rows are skipped here and exercised deliberately.
  - assert on the TILES, not on the document. The search query is echoed into
    the search box, so looking for a page's name in the whole page finds the
    box and reports a filter leak that is not there.

Run it with `ARTIFACTS_WALK=1 uv run pytest -q -s tests/test_walkthrough.py`.
"""

import re

from fastapi.testclient import TestClient

from artifacts import decisions, keys, store
from artifacts.app import create_gallery_app
from artifacts.db import Database

SAME = {"sec-fetch-site": "same-origin"}
findings = []


def note(where, what):
    findings.append(f"{where}: {what}")
    print(f"  FINDING  {where}: {what}")


def body(title, words):
    return (
        f"<!doctype html><html><head><title>{title}</title></head><body>"
        f'<p class="standfirst">{words}</p></body></html>'
    )


def fresh(dsn):
    db = Database(dsn)
    db.reset()
    keys.mint(db, agent="seat")
    return db, TestClient(create_gallery_app(db=db), base_url="https://testserver")


def forms_in(html):
    """(method, action, fields) for every form, honouring its own method."""
    out = []
    for m in re.finditer(r"<form([^>]*)>(.*?)</form>", html, re.DOTALL):
        attrs, inner = m.group(1), m.group(2)
        action = (re.search(r'action="([^"]*)"', attrs) or [None, ""])[1]
        method = (re.search(r'method="([^"]*)"', attrs) or [None, "get"])[1].lower()
        fields = dict(re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', inner))
        for name in re.findall(r'name="([^"]+)"', inner):
            fields.setdefault(name, "")
        out.append((method, action, fields))
    return out


def walk(dsn: str) -> list[str]:
    findings.clear()
    # --- journey 1: every link, on every view --------------------------------
    print("\n1. every link on every view resolves")
    db, c = fresh(dsn)
    live = store.publish(
        db, "seat", "Warpgate CI", body("Warpgate CI", "Where it stands.")
    )
    store.publish(db, "seat", "Photo backup", body("Photo backup", "21 % done."))
    gone = store.publish(db, "seat", "Old thing", body("Old", "x"))
    store.set_archived(db, gone.id, True, agent=None)
    decisions.ask(
        db,
        agent="seat",
        artifact_id=live.id,
        question="Which gate?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    for view in (
        "/",
        "/?view=list",
        "/?filter=waiting",
        "/?filter=pinned",
        "/?filter=deleted",
        f"/a/{live.id}",
    ):
        html = c.get(view).text
        for href in sorted(set(re.findall(r'href="([^"]+)"', html))):
            if href.startswith(("http", "mailto:")):
                continue
            got = c.get(href.split("#")[0] or view, follow_redirects=False)
            if got.status_code >= 400:
                note(view, f"link {href!r} -> {got.status_code}")

    # --- journey 2: every form, by its own method ----------------------------
    print("\n2. every form on every view submits")
    # Destroy with an empty box is a DESIGNED refusal, and delete/destroy would
    # remove the page the rest of the sweep is about -- the first version of
    # this walk clicked Delete and then reported three 404s it had caused
    # itself. Both are skipped here and exercised deliberately in journey 5.
    destructive = ("/delete", "/destroy")
    for view in ("/", "/?view=list", f"/a/{live.id}", "/?filter=deleted"):
        for method, action, fields in forms_in(c.get(view).text):
            if action.endswith(destructive):
                continue
            call = c.get if method == "get" else c.post
            kw = {"params": fields} if method == "get" else {"data": fields}
            got = call(action or view, headers=SAME, follow_redirects=False, **kw)
            if got.status_code >= 400:
                note(view, f"{method.upper()} {action} {fields} -> {got.status_code}")

    # --- journey 3: the rename dialog, end to end ----------------------------
    print("\n3. rename: menu -> dialog -> saved -> shown")
    db, c = fresh(dsn)
    a = store.publish(db, "seat", "Before", body("Before", "x"))
    wall = c.get("/").text
    if f'href="/a/{a.id}#rename"' not in wall:
        note("rename", "the wall's menu does not link to the dialog")
    page = c.get(f"/a/{a.id}").text
    if 'id="rename"' not in page:
        note("rename", "the dialog is not on the page the menu sends you to")
    if 'value="Before"' not in page:
        note("rename", "the dialog does not prefill the current name")
    saved = c.post(
        f"/a/{a.id}/rename",
        data={"title": "After"},
        headers=SAME,
        follow_redirects=True,
    )
    if "After" not in saved.text:
        note("rename", "the new name is not shown on the page you land on")
    if "After" not in c.get("/").text:
        note("rename", "the new name is not on the wall")

    # --- journey 4: delete from the wall, then recover -----------------------
    print("\n4. delete from the wall -> land in the bin -> put back")
    db, c = fresh(dsn)
    a = store.publish(db, "seat", "Doomed", body("Doomed", "x"))
    got = c.post(f"/a/{a.id}/delete", data={}, headers=SAME, follow_redirects=False)
    dest = got.headers.get("location", "")
    landed = c.get(dest)
    if "Doomed" not in landed.text:
        note("delete", f"landed on {dest!r} and the deleted page is not shown there")
    if "Put back" not in landed.text:
        note("delete", "no way back offered where you land")
    for _method, action, fields in forms_in(landed.text):
        if action.endswith("/restore"):
            back = c.post(action, data=fields, headers=SAME, follow_redirects=True)
            if "Doomed" not in back.text:
                note("restore", "restoring did not show the page again")
            break
    else:
        note("delete", "no restore form where you land")

    # --- journey 5: destroy, including getting it wrong ----------------------
    print("\n5. destroy: wrong word, empty, right word")
    db, c = fresh(dsn)
    for word in ("", "   ", "yes", "delete", "destroyy", "DESTROY ", "destroy"):
        # A FRESH page for each word. Sharing one meant every word after the
        # successful "DESTROY " saw an already-empty bin and was reported as
        # having destroyed something -- the walk accusing itself.
        a = store.publish(db, "seat", f"For good {word!r}", body("For good", "x"))
        store.set_deleted(db, a.id, True, agent=None)
        before = {r.id for r in store.list_deleted(db)}
        got = c.post(
            f"/a/{a.id}/destroy",
            data={"confirm": word},
            headers=SAME,
            follow_redirects=False,
        )
        after = {r.id for r in store.list_deleted(db)}
        destroyed = a.id in before and a.id not in after
        if word.strip().lower() == "destroy":
            if not destroyed:
                note("destroy", f"confirm={word!r} should have destroyed it")
        elif destroyed:
            note("destroy", f"confirm={word!r} DESTROYED IT")
        elif got.status_code not in (400, 422):
            note(
                "destroy", f"confirm={word!r} -> {got.status_code}, expected a refusal"
            )
        store.destroy(db, a.id, agent=None)

    # --- journey 6: duplicate, then work on the copy -------------------------
    print("\n6. duplicate -> land on the copy -> rename the copy")
    db, c = fresh(dsn)
    a = store.publish(db, "seat", "Original", body("Original", "x"))
    got = c.post(f"/a/{a.id}/duplicate", data={}, headers=SAME, follow_redirects=False)
    dest = got.headers.get("location", "")
    if dest == f"/a/{a.id}":
        note("duplicate", "landed back on the original, not the copy")
    copy_page = c.get(dest)
    if copy_page.status_code != 200:
        note("duplicate", f"the copy's page is {copy_page.status_code}")
    elif "(copy)" not in copy_page.text:
        note("duplicate", "the copy is not named as a copy where you land")
    copy_id = dest.rsplit("/", 1)[-1]
    r = c.post(
        f"/a/{copy_id}/rename",
        data={"title": "Renamed copy"},
        headers=SAME,
        follow_redirects=True,
    )
    if "Renamed copy" not in r.text:
        note("duplicate", "the copy cannot be renamed")

    # --- journey 7: the view choice survives a journey -----------------------
    print("\n7. the view he chose survives going into a page and back")
    db, c = fresh(dsn)
    a = store.publish(db, "seat", "Sticky", body("Sticky", "x"))
    c.get("/?view=list")
    c.get(f"/a/{a.id}")
    if '<ul class="rows">' not in c.get("/").text:
        note("view", "coming back from a page forgets the list view")
    # A filter with nothing in it renders the empty message, not a list -- so
    # ask a filter that HAS something, or the walk is testing emptiness.
    store.set_pinned(db, a.id, True, agent=None)
    if '<ul class="rows">' not in c.get("/?filter=pinned").text:
        note("view", "a filter tab forgets the list view")
    if '<ul class="rows">' not in c.get("/?q=Sticky").text:
        note("view", "searching forgets the list view")

    # --- journey 8: a stale wall ---------------------------------------------
    print("\n8. a stale tab: acting on a page that has since gone")
    db, c = fresh(dsn)
    a = store.publish(db, "seat", "Vanishing", body("Vanishing", "x"))
    stale = c.get("/").text
    store.set_deleted(db, a.id, True, agent=None)  # deleted in another tab
    for _method, action, fields in forms_in(stale):
        if action.endswith(("/pin", "/archive", "/rename", "/duplicate")):
            got = c.post(action, data=fields, headers=SAME, follow_redirects=False)
            if got.status_code >= 500:
                note(
                    "stale tab",
                    f"{action} -> {got.status_code} (a crash, not a refusal)",
                )

    # --- journey 9: the last page --------------------------------------------
    print("\n9. the wall with nothing on it")
    db, c = fresh(dsn)
    empty = c.get("/")
    if empty.status_code != 200:
        note("empty wall", f"-> {empty.status_code}")
    if "No pages published yet" not in empty.text:
        note("empty wall", "says nothing about being empty")
    a = store.publish(db, "seat", "Only one", body("Only one", "x"))
    store.set_archived(db, a.id, True, agent=None)
    only_archived = c.get("/").text
    if "Nothing on the wall" not in only_archived:
        note("empty wall", "everything archived, but the wall does not say so")

    # --- journey 10: search inside a filter ----------------------------------
    print("\n10. search combined with a filter")
    db, c = fresh(dsn)
    a = store.publish(db, "seat", "Has question", body("Has question", "x"))
    store.publish(db, "seat", "Quiet", body("Quiet", "x"))
    decisions.ask(
        db,
        agent="seat",
        artifact_id=a.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    both = c.get("/?filter=waiting&q=Quiet").text
    # The query is echoed into the search box's value, so looking for the word
    # in the whole document finds the box. Look at the TILES.
    shown = re.findall(r'<span class="t">([^<]*)</span>', both)
    if "Quiet" in shown:
        note("search+filter", "a page outside the filter appears in its results")
    if "Nothing here matches" not in both:
        note("search+filter", "an empty combined result says nothing")
    # And the positive case: a search inside a filter that DOES match.
    hit = c.get("/?filter=waiting&q=Has").text
    if "Has question" not in re.findall(r'<span class="t">([^<]*)</span>', hit):
        note("search+filter", "a page inside the filter does NOT appear in its results")

    # --- journey 11: a question on a page he then tidies away ---------------
    print("\n11. an unanswered question on a page that gets archived or deleted")
    for tidy, label in (
        (lambda db, i: store.set_archived(db, i, True, agent=None), "archived"),
        (lambda db, i: store.set_deleted(db, i, True, agent=None), "deleted"),
    ):
        db, c = fresh(dsn)
        a = store.publish(db, "seat", "Asks something", body("Asks", "x"))
        decisions.ask(
            db,
            agent="seat",
            artifact_id=a.id,
            question="Which?",
            options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
            recommended="a",
        )
        if "1 question waiting on you" not in c.get("/").text:
            note("waiting", "an open question is not announced before tidying")
        tidy(db, a.id)
        wall = c.get("/").text
        still_open = decisions.list_for_artifact(db, a.id)
        # Either the ordinary pill or the "tidied away" line -- both are the
        # gallery telling him the question is still open somewhere.
        announced = "waiting on you" in wall or "still unanswered" in wall
        if still_open and not announced:
            note(
                f"{label} + open question",
                "the question is still unanswered and the wall no longer mentions "
                "it -- the agent that asked waits for ever and he never sees it",
            )

    # --- journey 12: pressing the same button twice --------------------------
    print("\n12. double submit")
    db, c = fresh(dsn)
    a = store.publish(db, "seat", "Twice", body("Twice", "x"))
    for action, data in (
        (f"/a/{a.id}/pin", {"pinned": "1"}),
        (f"/a/{a.id}/archive", {"archived": "1"}),
        (f"/a/{a.id}/duplicate", {}),
    ):
        first = c.post(action, data=data, headers=SAME, follow_redirects=False)
        second = c.post(action, data=data, headers=SAME, follow_redirects=False)
        if second.status_code >= 500:
            note("double submit", f"{action} twice -> {second.status_code}")
        if action.endswith("/duplicate") and first.headers.get(
            "location"
        ) == second.headers.get("location"):
            note("double submit", "duplicating twice returned the same copy")
    copies = [r for r in store.list_for_agent(db, "seat") if "(copy)" in r.title]
    if len(copies) != 2:
        note("double submit", f"two duplicates made {len(copies)} copies")

    # --- journey 13: awkward text --------------------------------------------
    print("\n13. awkward titles")
    db, c = fresh(dsn)
    for title in (
        "<script>alert(1)</script>",
        "Ünïcödé — em–dash & ampersand",
        "مرحبا بالعالم",
        "A" * 200,
        "  leading and trailing  ",
    ):
        made = store.publish(db, "seat", title, body("x", "y"))
        wall = c.get("/")
        if wall.status_code != 200:
            note("awkward title", f"{title[:24]!r} broke the wall ({wall.status_code})")
        if "<script>alert(1)</script>" in wall.text:
            note("awkward title", "a script tag in a title is not escaped")
        detail = c.get(f"/a/{made.id}")
        if detail.status_code != 200:
            note(
                "awkward title", f"{title[:24]!r} broke its page ({detail.status_code})"
            )

    # --- journey 14: a wall with a lot on it ---------------------------------
    print("\n14. sixty pages")
    db, c = fresh(dsn)
    for n in range(60):
        store.publish(db, "seat", f"Page {n:02d}", body(f"Page {n}", "x"))
    big = c.get("/")
    shown = len(re.findall(r'<li class="tile-wrap', big.text))
    if big.status_code != 200:
        note("sixty pages", f"the wall is {big.status_code}")
    if shown != 60:
        note("sixty pages", f"{shown} tiles rendered for 60 pages")
    if len(big.text) > 900_000:
        note("sixty pages", f"the wall is {len(big.text) // 1024} KB")

    print(f"\n=== {len(findings)} findings ===")
    for f in findings:
        print(" -", f)
    return list(findings)
