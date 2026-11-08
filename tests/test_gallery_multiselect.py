"""The wall's multi-select (docs/agent/specs/2026-11-02-folders-design.md 3.3, 8.3b).

`?select=1` draws a checkbox per page inside one form; POST /select moves the ticked pages
into one of his folders, a new folder, or out of their folders, in one transaction. His, so
behind the same-origin guard; agent twin: POST /api/v1/artifacts/move."""

import pytest
from fastapi.testclient import TestClient

from ream import store
from ream.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><h1>Hi</h1></body></html>"
)
SAME = {"origin": store.gallery_origin(), "sec-fetch-site": "same-origin"}
CROSS = {"origin": store.gallery_origin(), "sec-fetch-site": "cross-site"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def put(db, agent, title, folder=None, folder_title=None):
    return store.publish(
        db, agent, title, PAGE, folder=folder, folder_title=folder_title
    )


def select(client, pages, headers=SAME, **data):
    return client.post(
        "/select",
        data={"page": [p.id if hasattr(p, "id") else p for p in pages], **data},
        headers=headers,
        follow_redirects=False,
    )


def folder_of(db, page_id):
    with db.connect() as conn:
        return conn.execute(
            "select f.id, f.owner, f.title from artifact a join folder f"
            " on f.id = a.folder_id where a.id = %s",
            (page_id,),
        ).fetchone()


def his_folder(client, db, page, title="Roadmap"):
    r = client.post(
        f"/a/{page.id}/folder",
        data={"new_title": title},
        headers=SAME,
        follow_redirects=False,
    )
    assert r.status_code == 303
    return folder_of(db, page.id)[0]


def count(db, sql, *params):
    with db.connect() as conn:
        return conn.execute(sql, params).fetchone()[0]


def test_select_mode_renders_a_checkbox_per_page_in_grid_and_list_not_on_folders(
    client, db
):
    p, q = put(db, "alpha", "Loose"), put(db, "beta", "Other")
    held = put(db, "alpha", "Held", folder="digests", folder_title="Digests")
    plain = client.get("/").text
    assert 'name="page"' not in plain and ">Select</a>" in plain
    for view in ("grid", "list"):
        html = client.get(f"/?select=1&view={view}").text
        assert f'name="page" value="{p.id}"' in html
        assert f'name="page" value="{q.id}"' in html
        assert (
            f'value="{held.id}"' not in html
        )  # behind its folder tile, and a tile has none
        assert 'action="/select"' in html and "Take out of folder" in html
        assert ">Done</a>" in html
        assert "Select Loose" in html
        # no nested forms: pin and the dots are replaced by the tick
        assert f'action="/a/{p.id}/pin"' not in html
    fid = folder_of(db, held.id)[0]
    assert f'value="{held.id}"' in client.get(f"/f/{fid}?select=1").text
    assert f'value="{held.id}"' in client.get(f"/f/{fid}?select=1&view=list").text


def test_the_tick_and_the_agent_filter_do_not_share_a_class(client, db):
    """Both were `.pick`; the select-mode rule (position:absolute) came later in the
    sheet and pulled the agent filter out of flow, over the search box, and showed it on
    a phone. Two controls, two classes."""
    import re

    put(db, "alpha", "One")
    put(db, "beta", "Two")
    html = client.get("/?select=1&view=grid").text
    assert '<label class="sel-pick">' in html
    assert '<label class="pick"' not in html
    assert '<details class="menu pick' in html
    for rule in re.findall(r"([^{}]+)\{([^{}]*)\}", html):
        selector, body = rule
        if "position:absolute" in body.replace(" ", ""):
            assert not re.search(r"\.pick(?![\w-])", selector), selector


def test_a_get_changes_nothing(client, db):
    p = put(db, "alpha", "P")
    client.get("/?select=1")
    assert count(db, "select folder_id from artifact where id = %s", p.id) is None


def test_choose_page_lists_his_folders_and_the_one_agents_and_moves_nothing(client, db):
    p, q = put(db, "alpha", "P"), put(db, "alpha", "Q")
    mine = his_folder(client, db, put(db, "beta", "Seed"), "Mine")
    put(db, "alpha", "Z", folder="alpha-f", folder_title="Alpha F")
    put(db, "beta", "Y", folder="beta-f", folder_title="Beta F")
    got = select(client, [p, q], do="choose", back="/")
    assert got.status_code == 200
    assert "Move 2 pages to a folder" in got.text
    assert "Mine" in got.text and "Alpha F" in got.text and "Beta F" not in got.text
    assert f'value="{mine}"' in got.text and 'name="new_title"' in got.text
    assert count(db, "select count(*) from artifact where folder_id is not null") == 3


def test_batch_move_into_his_folder_is_one_request(client, db):
    seed = put(db, "alpha", "Seed")
    fid = his_folder(client, db, seed)
    a, b = put(db, "alpha", "A"), put(db, "beta", "B")
    got = select(client, [a, b], do="move", folder=fid, back="/")
    assert got.status_code == 303
    assert got.headers["location"].startswith("/?select=1&done=moved&n=2")
    assert folder_of(db, a.id)[0] == fid and folder_of(db, b.id)[0] == fid
    assert folder_of(db, b.id)[1] == "jeremy"


def test_batch_move_into_a_new_folder_makes_one_folder(client, db):
    a, b = put(db, "alpha", "A"), put(db, "beta", "B")
    got = select(client, [a, b], do="move", new_title="  Sprint <b>1</b> ", back="/")
    assert got.status_code == 303
    fa, fb = folder_of(db, a.id), folder_of(db, b.id)
    assert fa == fb and fa[1] == "jeremy" and fa[2] == "Sprint <b>1</b>"
    assert count(db, "select count(*) from folder") == 1
    assert select(client, [a], do="move", new_title="  ", back="/").status_code == 422


def test_take_out_of_folder(client, db):
    a = put(db, "alpha", "A", folder="digests", folder_title="Digests")
    b = put(db, "alpha", "B", folder="digests", folder_title="Digests")
    fid = folder_of(db, a.id)[0]
    got = select(client, [a, b], do="out", back=f"/f/{fid}")
    assert got.status_code == 303 and got.headers["location"].startswith(f"/f/{fid}?")
    assert folder_of(db, a.id) is None and folder_of(db, b.id) is None


def test_mixed_agents_may_only_go_into_his_folders(client, db):
    a, b = put(db, "alpha", "A"), put(db, "beta", "B")
    theirs = folder_of(
        db, put(db, "alpha", "Z", folder="alpha-f", folder_title="Alpha F").id
    )[0]
    assert select(client, [a, b], do="move", folder=theirs).status_code == 404
    assert folder_of(db, a.id) is None and folder_of(db, b.id) is None
    # one agent's pages may go into that agent's own folder
    c = put(db, "alpha", "C")
    assert select(client, [a, c], do="move", folder=theirs).status_code == 303
    assert folder_of(db, c.id)[0] == theirs
    # and the batch is all or nothing: a gone page moves none
    d = put(db, "alpha", "D")
    assert select(client, [d, "nope"], do="move", folder=theirs).status_code == 404
    assert folder_of(db, d.id) is None


def test_an_archived_folder_is_refused_with_409_and_nothing_moves(client, db):
    seed = put(db, "alpha", "Seed")
    fid = his_folder(client, db, seed)
    client.post(
        f"/f/{fid}/archive",
        data={"archived": "1", "confirm": "1"},
        headers=SAME,
        follow_redirects=False,
    )
    a = put(db, "alpha", "A")
    got = select(client, [a], do="move", folder=fid)
    assert got.status_code == 409 and "archived" in got.text
    assert folder_of(db, a.id) is None
    assert "Archived" not in select(client, [a], do="choose").text.split("<h2>New")[0]


def test_cross_origin_is_refused(client, db):
    a = put(db, "alpha", "A")
    for do in ("choose", "out", "move"):
        got = select(client, [a], headers=CROSS, do=do, new_title="X")
        assert got.status_code == 403
    assert count(db, "select count(*) from folder") == 0


def test_the_batch_is_capped_at_200(client, db):
    a = put(db, "alpha", "A")
    ids = [f"id{i}" for i in range(201)]
    assert select(client, ids, do="out").status_code == 422
    assert select(client, ids, do="choose").status_code == 422
    ok = select(client, [a, *ids[:5]], do="choose")
    assert ok.status_code == 404  # unknown ids, not the cap
    assert store.MAX_FILE_BATCH == 200


def test_nothing_ticked_goes_back_with_a_note_and_back_is_never_an_open_redirect(
    client, db
):
    got = select(client, [], do="out", back="//evil.example/x")
    assert got.status_code == 303
    assert got.headers["location"].startswith("/?select=1&done=none&n=0")
    a = put(db, "alpha", "A")
    got = select(client, [a], do="out", back="https://evil.example/")
    assert got.headers["location"].startswith("/?")


def test_free_text_and_unknown_codes_render_nothing(client):
    for q in ("note=hello+world", "done=hello+world&n=3", "done=<script>x</script>"):
        html = client.get(f"/?select=1&{q}").text
        assert "hello world" not in html and "<script>x</script>" not in html
        assert 'role="status"' not in html


def test_known_codes_render_their_sentence_with_a_clamped_count(client):
    html = client.get("/?select=1&done=moved&n=3").text
    assert "Moved 3 pages into the folder." in html
    assert (
        "Moved 1 page out of their folders."
        in client.get("/?select=1&done=out&n=1").text
    )
    assert (
        "Moved 3 pages into a new folder." in client.get("/?select=1&done=new&n=3").text
    )
    assert "Moved 200 pages" in client.get("/?select=1&done=out&n=99999").text
    assert "Moved 0 pages" in client.get("/?select=1&done=out&n=-5").text


def test_out_never_creates_or_chooses_a_folder(client, db):
    a = put(db, "alpha", "A", folder="digests", folder_title="Digests")
    seed = put(db, "alpha", "Seed")
    fid = his_folder(client, db, seed)
    before = count(db, "select count(*) from folder")
    got = select(client, [a], do="out", new_title="Sneaky", folder=fid, back="/")
    assert got.status_code == 303 and "done=out&n=1" in got.headers["location"]
    assert folder_of(db, a.id) is None
    assert count(db, "select count(*) from folder") == before
