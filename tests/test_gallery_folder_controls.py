"""His folder controls on the wall (docs/agent/specs/2026-11-02-folders-design.md 3.3, 3.4, 8.1a).

Move to folder, Take out of folder, Rename folder, Archive folder (asks first), Unarchive. His,
so unscoped, behind the same-origin guard like pin and archive. A folder he makes is owned by
the reserved `jeremy` and holds any agent's pages; agents cannot publish into it."""

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, events, keys, store
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><h1>Hi</h1></body></html>"
)
SAME = {"origin": store.gallery_origin(), "sec-fetch-site": "same-origin"}
CROSS = {"origin": store.gallery_origin(), "sec-fetch-site": "cross-site"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def put(db, agent, title, folder=None, folder_title=None):
    return store.publish(
        db, agent, title, PAGE, folder=folder, folder_title=folder_title
    )


def ask(db, agent, page, q="Which?"):
    decisions.ask(
        db,
        agent=agent,
        artifact_id=page.id,
        question=q,
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )


def post(client, path, headers=SAME, **data):
    return client.post(path, data=data, headers=headers, follow_redirects=False)


def folder_of(db, page_id):
    with db.connect() as conn:
        return conn.execute(
            "select f.id, f.owner, f.title from artifact a join folder f"
            " on f.id = a.folder_id where a.id = %s",
            (page_id,),
        ).fetchone()


def kinds(db, agent):
    return [r.kind for r in events.list_for_agent(db, agent, since=0, limit=100)]


def count(db, sql, *params):
    with db.connect() as conn:
        return conn.execute(sql, params).fetchone()[0]


def his_folder(client, db, page, title="Roadmap"):
    assert post(client, f"/a/{page.id}/folder", new_title=title).status_code == 303
    return folder_of(db, page.id)


def test_tile_menu_offers_move_to_folder_and_inside_one_take_out(client, db):
    p = put(db, "alpha", "Loose")
    html = client.get("/").text
    assert f'href="/a/{p.id}/folder"' in html and "Move to folder" in html
    assert "Take out of folder" not in html
    fid = his_folder(client, db, p)[0]
    assert "Take out of folder" in client.get(f"/f/{fid}").text
    assert "Take out of folder" in client.get(f"/f/{fid}?view=list").text


def test_a_new_folder_of_his_is_owned_by_jeremy_and_holds_any_agents_pages(client, db):
    a, b = put(db, "alpha", "From alpha"), put(db, "beta", "From beta")
    fid, owner, title = his_folder(client, db, a, "Cove <b>work</b>")
    assert owner == "jeremy" and title == "Cove <b>work</b>"
    choices = client.get(f"/a/{b.id}/folder").text
    assert f'value="{fid}"' in choices and "yours" in choices
    assert "<b>work</b>" not in choices and "&lt;b&gt;work&lt;/b&gt;" in choices
    assert post(client, f"/a/{b.id}/folder", folder=fid).status_code == 303
    assert folder_of(db, b.id)[0] == fid
    inside = client.get(f"/f/{fid}").text
    assert "From alpha" in inside and "From beta" in inside
    assert "alpha" in inside and "beta" in inside, "each page keeps its publisher"
    wall = client.get("/").text
    assert "2 pages" in wall and "alpha, beta" in wall
    assert "<b>work</b>" not in wall


def test_choices_list_his_folders_and_the_pages_own_agents_but_not_others(client, db):
    a = put(db, "alpha", "A", folder="alpha-things", folder_title="Alpha things")
    put(db, "beta", "B", folder="beta-things", folder_title="Beta things")
    loose = put(db, "alpha", "Loose")
    his_fid = his_folder(client, db, put(db, "beta", "seed"), "Mine")[0]
    html = client.get(f"/a/{loose.id}/folder").text
    assert "Alpha things" in html and "Mine" in html and "Beta things" not in html
    assert f'value="{his_fid}"' in html
    beta_fid = folder_of(db, put(db, "beta", "B2", folder="beta-things").id)[0]
    assert post(client, f"/a/{a.id}/folder", folder=beta_fid).status_code == 404
    assert folder_of(db, a.id)[1] == "alpha"


def test_take_out_of_folder_goes_to_the_wall(client, db):
    p = put(db, "alpha", "Filed")
    his_folder(client, db, p)
    got = post(client, f"/a/{p.id}/folder", folder="")
    assert got.status_code == 303 and got.headers["location"] == "/"
    assert folder_of(db, p.id) is None
    assert "Filed" in client.get("/").text
    assert count(db, "select count(*) from version") == 1, "a shelf fact: no version"


def test_a_second_new_folder_with_the_same_name_is_another_folder(client, db):
    a, b = put(db, "alpha", "A"), put(db, "alpha", "B")
    one, two = his_folder(client, db, a)[0], his_folder(client, db, b)[0]
    assert one != two
    with db.connect() as conn:
        slugs = sorted(r[0] for r in conn.execute("select slug from folder").fetchall())
    assert slugs == ["roadmap", "roadmap-2"]


def test_an_empty_or_oversize_folder_name_is_refused(client, db):
    p = put(db, "alpha", "A")
    assert post(client, f"/a/{p.id}/folder", new_title="   ").status_code == 422
    assert post(client, f"/a/{p.id}/folder", new_title="x" * 5000).status_code == 422
    assert folder_of(db, p.id) is None


def test_moving_into_an_archived_folder_is_409_and_unknown_ids_404(client, db):
    p, q = put(db, "alpha", "P"), put(db, "alpha", "Q")
    fid = his_folder(client, db, p)[0]
    assert (
        post(client, f"/f/{fid}/archive", archived="1", confirm="1").status_code == 303
    )
    assert post(client, f"/a/{q.id}/folder", folder=fid).status_code == 409
    assert post(client, f"/a/{q.id}/folder", folder="nope").status_code == 404
    assert post(client, "/a/nope/folder", folder="").status_code == 404


def test_rename_folder_changes_the_title_and_escapes_it(client, db):
    p = put(db, "alpha", "P", folder="digests", folder_title="Digests")
    fid = folder_of(db, p.id)[0]
    assert 'value="Digests"' in client.get(f"/f/{fid}/rename").text
    got = post(client, f"/f/{fid}/rename", title="  Daily <i>news</i>  ")
    assert got.status_code == 303 and got.headers["location"] == f"/f/{fid}"
    assert folder_of(db, p.id)[2] == "Daily <i>news</i>"
    assert count(db, "select slug from folder") == "digests"
    wall = client.get("/").text
    assert "Daily &lt;i&gt;news&lt;/i&gt;" in wall and "<i>news</i>" not in wall
    assert (
        'value="Daily &lt;i&gt;news&lt;/i&gt;"' in client.get(f"/f/{fid}/rename").text
    )
    assert post(client, f"/f/{fid}/rename", title=" ").status_code == 422
    assert post(client, "/f/nope/rename", title="x").status_code == 404


def test_folder_menu_has_rename_and_archive_and_unarchive_in_the_archived_tab(
    client, db
):
    p = put(db, "alpha", "P", folder="digests")
    fid = folder_of(db, p.id)[0]
    wall = client.get("/").text
    assert f'href="/f/{fid}/rename"' in wall and f'href="/f/{fid}/archive"' in wall
    assert "Rename folder" in wall and "Archive folder" in wall
    assert "Unarchive" not in wall
    assert "Rename folder" in client.get("/?view=list").text
    post(client, f"/f/{fid}/archive", archived="1", confirm="1")
    tab = client.get("/?filter=archived").text
    assert "Unarchive" in tab and "Archive folder" not in tab
    assert f'action="/f/{fid}/archive"' in tab
    assert post(client, f"/f/{fid}/archive", archived="0").status_code == 303
    assert "P" in client.get(f"/f/{fid}").text
    assert count(db, "select archived_at from folder") is None


def test_the_archive_sheet_counts_come_from_the_preview_and_change_nothing(client, db):
    one = put(db, "alpha", "One", folder="f", folder_title="Roadmap")
    two = put(db, "alpha", "Two", folder="f")
    ask(db, "alpha", one)
    ask(db, "alpha", one, "Also?")
    ask(db, "alpha", two)
    fid = folder_of(db, one.id)[0]
    html = client.get(f"/f/{fid}/archive").text
    assert "Archive &lsquo;Roadmap&rsquo;?" in html
    assert "2 pages go to Archived." in html
    assert "3 questions waiting on you will be set aside" in html
    assert "the agents that asked are told; they come back when you unarchive." in html
    assert 'name="confirm" value="1"' in html and ">Archive</button>" in html
    assert f'href="/f/{fid}">Cancel' in html
    assert "confirm(" not in html
    assert count(db, "select archived_at from folder") is None
    assert count(db, "select count(*) from artifact where archived_at is not null") == 0


def test_the_archive_sheet_with_no_questions_still_shows_the_page_count(client, db):
    p = put(db, "alpha", "Only", folder="f")
    html = client.get(f"/f/{folder_of(db, p.id)[0]}/archive").text
    assert "1 page goes to Archived." in html and "No questions are waiting" in html
    assert "set aside" not in html


def test_the_sheet_escapes_the_title(client, db):
    p = put(db, "alpha", "P", folder="f", folder_title="<script>x</script>")
    html = client.get(f"/f/{folder_of(db, p.id)[0]}/archive").text
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html


def test_archiving_via_the_gallery_sets_questions_aside_and_tells_the_agent(client, db):
    one = put(db, "alpha", "One", folder="f")
    ask(db, "alpha", one)
    fid = folder_of(db, one.id)[0]
    got = post(client, f"/f/{fid}/archive", archived="1")
    assert got.status_code == 303 and got.headers["location"] == f"/f/{fid}/archive"
    assert "decision_set_aside" not in kinds(db, "alpha")
    assert (
        post(client, f"/f/{fid}/archive", archived="1", confirm="1").status_code == 303
    )
    assert kinds(db, "alpha").count("decision_set_aside") == 1
    assert count(db, "select archived_at from folder") is not None
    assert count(db, "select archived_at from artifact") is not None
    assert "waiting on you" not in client.get("/").text
    post(client, f"/f/{fid}/archive", archived="0")
    assert count(db, "select archived_at from artifact") is None
    assert "waiting" in client.get("/").text


def test_archiving_his_mixed_folder_archives_every_agents_page(client, db):
    a, b = put(db, "alpha", "A"), put(db, "beta", "B")
    fid = his_folder(client, db, a)[0]
    post(client, f"/a/{b.id}/folder", folder=fid)
    ask(db, "beta", b)
    html = client.get(f"/f/{fid}/archive").text
    assert "2 pages go to Archived." in html
    assert "1 question waiting on you will be set aside" in html
    post(client, f"/f/{fid}/archive", archived="1", confirm="1")
    assert count(db, "select count(*) from artifact where archived_at is not null") == 2
    assert "decision_set_aside" in kinds(db, "beta")
    post(client, f"/f/{fid}/archive", archived="0")
    assert count(db, "select count(*) from artifact where archived_at is not null") == 0


@pytest.mark.parametrize(
    "path,data",
    [
        ("/a/{p}/folder", {"new_title": "X"}),
        ("/a/{p}/folder", {"folder": ""}),
        ("/f/{f}/rename", {"title": "X"}),
        ("/f/{f}/archive", {"archived": "1", "confirm": "1"}),
        ("/f/{f}/archive", {"archived": "0"}),
    ],
)
def test_every_folder_control_refuses_a_cross_origin_post(client, db, path, data):
    p = put(db, "alpha", "P", folder="f", folder_title="Keep")
    fid = folder_of(db, p.id)[0]
    url = path.format(p=p.id, f=fid)
    assert client.post(url, data=data, headers=CROSS).status_code == 403
    assert client.post(url, data=data).status_code == 403
    assert folder_of(db, p.id) == (fid, "alpha", "Keep")
    assert count(db, "select archived_at from folder") is None


def test_agents_cannot_publish_into_his_folder_or_see_or_change_it(client, db):
    token = keys.mint(db, agent="alpha")[0]
    auth = {"Authorization": f"Bearer {token}"}
    seed = put(db, "beta", "seed")
    fid = his_folder(client, db, seed, "Mine")[0]
    # publishing with his slug makes alpha's OWN folder of that slug, never his
    mine = put(db, "alpha", "Mine too", folder="mine")
    other = folder_of(db, mine.id)
    assert other[0] != fid and other[1] == "alpha"
    assert count(db, "select count(*) from artifact where folder_id = %s", fid) == 1
    listed = client.get("/api/v1/folders", headers=auth).json()
    assert [f["slug"] for f in listed] == ["mine"] and listed[0]["pages"] == 1
    # archiving "mine" through alpha's key archives alpha's folder, not his
    got = client.post(
        "/api/v1/folders/mine/archive",
        json={"archived": True, "confirm": True},
        headers=auth,
    )
    assert got.json()["pages"] == 1
    assert folder_of(db, seed.id)[0] == fid
    assert count(db, "select archived_at from folder where id = %s", fid) is None
