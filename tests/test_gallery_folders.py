"""The read side of folders on the wall (docs/agent/specs/2026-11-02-folders-design.md 3.3).

A folder is one tile or row, its pages leave the grid, `/f/{id}` opens it, and any question put
to the wall (a search, an agent, Waiting, Pinned) lists pages flat."""

import re

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, store
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><h1>Hi</h1></body></html>"
)


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def put(db, agent, title, folder=None, folder_title=None):
    return store.publish(
        db, agent, title, PAGE, folder=folder, folder_title=folder_title
    )


def ask(db, agent, page):
    decisions.ask(
        db,
        agent=agent,
        artifact_id=page.id,
        question="Which?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )


def folder_id(db, slug):
    with db.connect() as conn:
        return conn.execute(
            "select id from folder where slug = %s", (slug,)
        ).fetchone()[0]


def test_foldered_pages_leave_the_grid_and_the_folder_is_one_tile(client, db):
    put(db, "bosun", "Digest one", folder="digests", folder_title="Daily digests")
    put(db, "bosun", "Digest two", folder="digests")
    put(db, "seat-b", "Loose page")
    html = client.get("/").text
    assert "Loose page" in html
    assert "Digest one" not in html and "Digest two" not in html
    fid = folder_id(db, "digests")
    assert f'href="/f/{fid}"' in html
    assert "Daily digests" in html and "2 pages" in html and "bosun" in html
    assert "waiting</span>" not in html
    assert "Edited" in html


def test_a_folder_in_list_view_is_one_row(client, db):
    put(db, "bosun", "Digest one", folder="digests")
    put(db, "bosun", "Digest two", folder="digests")
    html = client.get("/?view=list").text
    fid = folder_id(db, "digests")
    assert f'<a class="row" href="/f/{fid}">' in html
    assert "Digest one" not in html and "2 pages" in html


def test_waiting_counts_before_grouping_and_the_tile_says_so(client, db):
    one = put(db, "bosun", "Pg-asking", folder="digests")
    ask(db, "bosun", one)
    ask(db, "bosun", one)
    html = client.get("/").text
    assert "2 waiting" in html
    assert "2 questions waiting on you" in html
    assert "Pg-asking" not in html, "a waiting page shows only inside its folder"


def test_filter_waiting_lists_the_pages_flat(client, db):
    one = put(db, "bosun", "Pg-asking", folder="digests")
    ask(db, "bosun", one)
    put(db, "bosun", "Pg-quiet", folder="digests")
    html = client.get("/?filter=waiting").text
    assert "Pg-asking" in html and "Pg-quiet" not in html
    assert 'href="/f/' not in html


def test_a_folder_that_waits_sorts_among_waiting_items_above_newer_pages(client, db):
    old = put(db, "bosun", "Old asker", folder="digests", folder_title="Digests")
    ask(db, "bosun", old)
    put(db, "seat-b", "Newer loose")
    html = client.get("/").text
    assert html.index("Digests") < html.index("Newer loose")


def test_a_folder_sorts_by_its_newest_page_otherwise(client, db):
    put(db, "bosun", "Pg-early", folder="digests", folder_title="Digests")
    put(db, "seat-b", "Pg-middle")
    put(db, "bosun", "Pg-late", folder="digests")
    put(db, "seat-b", "Pg-latest")
    html = client.get("/").text
    assert html.index("Pg-latest") < html.index("Digests") < html.index("Pg-middle")


def test_an_open_folder_shows_its_pages_newest_first_with_waiting_above(client, db):
    put(db, "bosun", "Pg-alpha", folder="digests")
    asker = put(db, "bosun", "Pg-asker", folder="digests")
    put(db, "bosun", "Pg-omega", folder="digests")
    ask(db, "bosun", asker)
    fid = folder_id(db, "digests")
    html = client.get(f"/f/{fid}?view=list").text
    assert html.index("Pg-asker") < html.index("Pg-omega") < html.index("Pg-alpha")
    assert "1 question for you" in html
    assert f'action="/f/{fid}"' in html, "search stays inside the folder"
    grid = client.get(f"/f/{fid}?view=grid").text
    assert grid.index("Pg-asker") < grid.index("Pg-omega") < grid.index("Pg-alpha")
    assert "1 question for you" in grid


def test_an_open_folder_has_the_list_view_and_filters(client, db):
    one = put(db, "bosun", "Pg-alpha", folder="digests")
    put(db, "bosun", "Pg-beta", folder="digests")
    ask(db, "bosun", one)
    fid = folder_id(db, "digests")
    html = client.get(f"/f/{fid}?view=list&filter=waiting").text
    assert "Pg-alpha" in html and "Pg-beta" not in html
    assert '<a class="row" href="/a/' in html
    searched = client.get(f"/f/{fid}?q=pg-beta").text
    assert "Pg-beta" in searched and "Pg-alpha" not in searched


def test_an_unknown_folder_id_is_404_and_a_slug_is_not_an_id(client, db):
    put(db, "bosun", "x", folder="digests")
    assert client.get("/f/nope").status_code == 404
    assert client.get("/f/digests").status_code == 404


def test_a_folder_with_no_live_pages_is_not_drawn(client, db):
    page = put(db, "bosun", "Gone", folder="digests", folder_title="Empty shelf")
    store.set_deleted(db, page.id, True, agent="bosun")
    assert "Empty shelf" not in client.get("/").text
    page2 = put(db, "bosun", "Shelved", folder="other", folder_title="Other shelf")
    store.set_archived(db, page2.id, True, agent="bosun")
    assert "Other shelf" not in client.get("/").text


def test_a_search_or_agent_filter_finds_foldered_pages_flat(client, db):
    put(db, "bosun", "Needle digest", folder="digests", folder_title="Digests")
    put(db, "seat-b", "Other")
    by_q = client.get("/?q=needle").text
    assert "Needle digest" in by_q and 'href="/f/' not in by_q
    by_agent = client.get("/?agent=bosun").text
    assert "Needle digest" in by_agent and 'href="/f/' not in by_agent


def test_an_archived_folder_is_one_tile_in_the_archived_tab(client, db):
    put(db, "bosun", "In shelf", folder="digests", folder_title="Old digests")
    loose = put(db, "bosun", "Loose archived")
    store.set_archived(db, loose.id, True, agent="bosun")
    store.set_folder_archived(db, "bosun", "digests", True, confirm=True)
    fid = folder_id(db, "digests")
    html = client.get("/?filter=archived").text
    assert f'href="/f/{fid}"' in html and "Old digests" in html
    assert "In shelf" not in html
    assert "Loose archived" in html
    assert "Old digests" not in client.get("/").text
    assert "In shelf" in client.get(f"/f/{fid}").text


def test_a_page_archived_alone_out_of_a_live_folder_stays_in_the_flat_archive(
    client, db
):
    keep = put(db, "bosun", "Stays", folder="digests", folder_title="Live shelf")
    put(db, "bosun", "Other", folder="digests")
    store.set_archived(db, keep.id, True, agent="bosun")
    html = client.get("/?filter=archived").text
    assert "Stays" in html
    assert "Live shelf" not in html


def test_a_hostile_folder_title_is_escaped(client, db):
    put(db, "bosun", "p", folder="digests", folder_title="<script>alert(1)</script>")
    fid = folder_id(db, "digests")
    for url in ("/", "/?view=list", f"/f/{fid}"):
        html = client.get(url).text
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert re.search(r'href="/f/[^"<>]+"', client.get("/").text)


def test_empty_state_links_inside_a_folder_stay_in_the_folder(client, db):
    put(db, "bosun", "Pg-alpha", folder="digests")
    fid = folder_id(db, "digests")
    html = client.get(f"/f/{fid}?q=zzznomatch&view=list").text
    assert "Nothing here matches" in html
    assert f'<a href="/f/{fid}?view=list">Show everything</a>' in html
    assert '<a href="/?view=list">Show everything' not in html


def test_the_tabs_narrow_an_archived_folder_too(client, db):
    one = put(db, "bosun", "Pg-pinned", folder="digests")
    put(db, "bosun", "Pg-plain", folder="digests")
    store.set_pinned(db, one.id, True, agent="bosun")
    store.set_folder_archived(db, "bosun", "digests", True, confirm=True)
    fid = folder_id(db, "digests")
    both = client.get(f"/f/{fid}").text
    assert "Pg-pinned" in both and "Pg-plain" in both
    pinned = client.get(f"/f/{fid}?filter=pinned").text
    assert "Pg-pinned" in pinned and "Pg-plain" not in pinned
    waiting = client.get(f"/f/{fid}?filter=waiting").text
    assert "Pg-pinned" not in waiting and "Pg-plain" not in waiting
