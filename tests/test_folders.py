"""Folders, slice 1: an agent files pages in its own folders (docs/agent/specs/2026-11-02-folders-design.md).

What these hold to: a folder is made on first use and the result SAYS so (a typo must show); a
slug is validated; filing is a shelf fact that makes no version; a folder is its owner's alone and
"not yours" answers as "no such folder"; the listing counts pages and open questions; the wall is
untouched.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
PAGE_2 = PAGE.replace("<p>x</p>", "<p>y</p>")


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


@pytest.fixture()
def tokens(db):
    return {name: keys.mint(db, agent=name)[0] for name in ("alpha", "beta")}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def publish(client, token, title="P", **extra):
    return client.post(
        "/api/v1/artifacts",
        json={"title": title, "body": PAGE, **extra},
        headers=auth(token),
    )


# --- publish ------------------------------------------------------------------


def test_a_publish_creates_the_folder_on_first_use_and_says_so(client, tokens):
    first = publish(client, tokens["alpha"], folder="roadmap")
    assert first.status_code == 201
    assert first.json()["folder"] == {"slug": "roadmap", "created": True}
    second = publish(client, tokens["alpha"], title="Q", folder="roadmap")
    assert second.json()["folder"] == {"slug": "roadmap", "created": False}, (
        "only the call that made it says it did"
    )


def test_a_publish_without_a_folder_reports_none_and_stays_loose(client, tokens):
    got = publish(client, tokens["alpha"])
    assert got.json()["folder"] is None
    rows = client.get("/api/v1/artifacts", headers=auth(tokens["alpha"])).json()
    assert rows[0]["folder"] is None
    assert client.get("/api/v1/folders", headers=auth(tokens["alpha"])).json() == []


def test_the_folder_title_names_a_new_folder_once(client, tokens):
    publish(client, tokens["alpha"], folder="cps", folder_title="Cove public split")
    publish(client, tokens["alpha"], title="Q", folder="cps", folder_title="Renamed?")
    (row,) = client.get("/api/v1/folders", headers=auth(tokens["alpha"])).json()
    assert row["slug"] == "cps"
    assert row["title"] == "Cove public split", "set once, by the call that made it"


def test_the_title_defaults_to_the_slug(client, tokens):
    publish(client, tokens["alpha"], folder="roadmap")
    (row,) = client.get("/api/v1/folders", headers=auth(tokens["alpha"])).json()
    assert row["title"] == "roadmap"


@pytest.mark.parametrize(
    "slug", ["Road", "road map", "-lead", "a" * 41, "road_map", "résumé", "a/b"]
)
def test_a_bad_slug_is_a_422_naming_folder_and_stores_nothing(client, db, tokens, slug):
    got = publish(client, tokens["alpha"], folder=slug)
    assert got.status_code == 422
    assert got.json()["detail"]["field"] == "folder"
    assert client.get("/api/v1/artifacts", headers=auth(tokens["alpha"])).json() == []


def test_a_forty_character_slug_is_fine(client, tokens):
    assert publish(client, tokens["alpha"], folder="a" * 40).status_code == 201


def test_the_same_slug_is_another_folder_for_another_agent(client, tokens):
    a = publish(client, tokens["alpha"], folder="roadmap").json()
    b = publish(client, tokens["beta"], folder="roadmap").json()
    assert a["folder"]["created"] and b["folder"]["created"]
    assert (
        client.get("/api/v1/folders", headers=auth(tokens["alpha"])).json()[0]["pages"]
        == 1
    )


def test_a_folder_title_is_scanned_like_any_title(client, tokens):
    got = publish(
        client,
        tokens["alpha"],
        folder="roadmap",
        folder_title="AKIA" + "IOSFODNN7EXAMPLE",
    )
    assert got.status_code == 422
    assert got.json()["detail"]["refused"][0]["field"] == "folder_title"


# --- update -------------------------------------------------------------------


def _patch(client, token, page_id, revision, body=PAGE, **extra):
    return client.patch(
        f"/api/v1/artifacts/{page_id}",
        json={"body": body, "expected_revision": revision, **extra},
        headers=auth(token),
    )


def test_an_update_files_a_loose_page_and_an_empty_slug_takes_it_out(client, tokens):
    page = publish(client, tokens["alpha"]).json()
    filed = _patch(client, tokens["alpha"], page["id"], page["revision"], folder="x")
    assert filed.status_code == 200
    assert filed.json()["folder"] == {"slug": "x", "created": True}
    assert filed.json()["unchanged"] is True and filed.json()["version"] == 1, (
        "filing makes no version"
    )
    got = client.get(f"/api/v1/artifacts/{page['id']}", headers=auth(tokens["alpha"]))
    assert got.json()["folder"] == "x"

    out = _patch(client, tokens["alpha"], page["id"], page["revision"], folder="")
    assert out.json()["folder"] is None
    got = client.get(f"/api/v1/artifacts/{page['id']}", headers=auth(tokens["alpha"]))
    assert got.json()["folder"] is None


def test_an_update_that_omits_folder_leaves_it_and_a_new_version_can_file(
    client, tokens
):
    page = publish(client, tokens["alpha"], folder="x").json()
    kept = _patch(
        client, tokens["alpha"], page["id"], page["revision"], body=PAGE_2
    ).json()
    assert kept["version"] == 2 and kept["folder"] is None, "none named, none reported"
    row = client.get(f"/api/v1/artifacts/{page['id']}", headers=auth(tokens["alpha"]))
    assert row.json()["folder"] == "x", "and the page is still in it"

    moved = _patch(
        client, tokens["alpha"], page["id"], kept["revision"], body=PAGE, folder="y"
    ).json()
    assert moved["version"] == 3 and moved["folder"]["created"] is True


def test_a_bad_slug_on_update_changes_nothing(client, tokens):
    page = publish(client, tokens["alpha"]).json()
    got = _patch(
        client, tokens["alpha"], page["id"], page["revision"], body=PAGE_2, folder="No"
    )
    assert got.status_code == 422
    row = client.get(f"/api/v1/artifacts/{page['id']}", headers=auth(tokens["alpha"]))
    assert row.json()["current_version"] == 1


def test_an_archived_folder_takes_no_page_and_the_refusal_changes_nothing(
    client, db, tokens
):
    publish(client, tokens["alpha"], folder="old")
    with db.connect() as conn, conn.transaction():
        conn.execute("update folder set archived_at = %s", (datetime.now(UTC),))
    again = publish(client, tokens["alpha"], title="Q", folder="old")
    assert again.status_code == 409
    assert again.json()["detail"]["field"] == "folder"
    assert (
        len(client.get("/api/v1/artifacts", headers=auth(tokens["alpha"])).json()) == 1
    ), "the refused publish stored no page"

    page = publish(client, tokens["alpha"], title="loose").json()
    refused = _patch(
        client, tokens["alpha"], page["id"], page["revision"], body=PAGE_2, folder="old"
    )
    assert refused.status_code == 409
    row = client.get(f"/api/v1/artifacts/{page['id']}", headers=auth(tokens["alpha"]))
    assert row.json()["current_version"] == 1, "no version minted by a refused filing"


# --- listing ------------------------------------------------------------------


def test_folders_lists_pages_and_open_questions_for_the_caller_only(client, db, tokens):
    one = publish(client, tokens["alpha"], folder="roadmap").json()
    publish(client, tokens["alpha"], title="Q", folder="roadmap")
    publish(client, tokens["alpha"], title="loose")
    publish(client, tokens["beta"], title="theirs", folder="secret")
    decisions.ask(
        db,
        agent="alpha",
        artifact_id=one["id"],
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    got = client.get("/api/v1/folders", headers=auth(tokens["alpha"])).json()
    assert [
        (f["slug"], f["pages"], f["open_questions"], f["archived"]) for f in got
    ] == [("roadmap", 2, 1, False)]


def test_an_archived_page_questions_are_not_counted_as_waiting(client, db, tokens):
    one = publish(client, tokens["alpha"], folder="roadmap").json()
    decisions.ask(
        db,
        agent="alpha",
        artifact_id=one["id"],
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    client.post(
        f"/api/v1/artifacts/{one['id']}/archive",
        json={"archived": True},
        headers=auth(tokens["alpha"]),
    )
    (row,) = client.get("/api/v1/folders", headers=auth(tokens["alpha"])).json()
    assert row["pages"] == 1 and row["open_questions"] == 0, "set aside, not waiting"


def test_a_binned_page_is_not_counted_or_listed_and_restoring_puts_it_back(
    client, db, tokens
):
    one = publish(client, tokens["alpha"], folder="roadmap").json()
    publish(client, tokens["alpha"], title="stays", folder="roadmap")
    assert store.set_deleted(db, one["id"], True, agent="alpha")
    (row,) = client.get("/api/v1/folders", headers=auth(tokens["alpha"])).json()
    assert row["pages"] == 1
    pages = client.get(
        "/api/v1/folders/roadmap/artifacts", headers=auth(tokens["alpha"])
    )
    assert [r["title"] for r in pages.json()] == ["stays"]
    assert store.set_deleted(db, one["id"], False, agent="alpha")
    (row,) = client.get("/api/v1/folders", headers=auth(tokens["alpha"])).json()
    assert row["pages"] == 2, "binning leaves folder_id set"


def test_a_folders_pages_come_back_newest_first_in_the_listing_shape(client, tokens):
    first = publish(client, tokens["alpha"], title="first", folder="roadmap").json()
    second = publish(client, tokens["alpha"], title="second", folder="roadmap").json()
    publish(client, tokens["alpha"], title="loose")
    got = client.get("/api/v1/folders/roadmap/artifacts", headers=auth(tokens["alpha"]))
    assert got.status_code == 200
    assert [r["id"] for r in got.json()] == [second["id"], first["id"]]
    assert {"id", "title", "revision", "page_url", "folder"} <= set(got.json()[0])
    assert got.json()[0]["folder"] == "roadmap"


def test_not_yours_answers_exactly_like_no_such_folder(client, tokens):
    publish(client, tokens["alpha"], folder="roadmap")
    theirs = client.get(
        "/api/v1/folders/roadmap/artifacts", headers=auth(tokens["beta"])
    )
    nothing = client.get(
        "/api/v1/folders/nonesuch/artifacts", headers=auth(tokens["beta"])
    )
    assert theirs.status_code == nothing.status_code == 404
    assert theirs.json() == nothing.json()
    assert client.get("/api/v1/folders", headers=auth(tokens["beta"])).json() == []


def test_the_folder_routes_need_a_key(client):
    assert client.get("/api/v1/folders").status_code in (401, 403)
    assert client.get("/api/v1/folders/x/artifacts").status_code in (401, 403)


def test_another_agents_open_page_shows_no_folder_in_all_scope(client, tokens):
    publish(client, tokens["alpha"], folder="roadmap")
    rows = client.get(
        "/api/v1/artifacts?scope=all", headers=auth(tokens["beta"])
    ).json()
    assert len(rows) == 1 and rows[0]["folder"] is None
    mine = client.get(
        "/api/v1/artifacts?scope=all", headers=auth(tokens["alpha"])
    ).json()
    assert mine[0]["folder"] == "roadmap"


# --- the store, and what must not change --------------------------------------


def test_an_old_slug_resolves_through_the_alias_table(db):
    keys.mint(db, agent="alpha")
    page = store.publish(db, "alpha", "P", PAGE, folder="roadmap")
    assert page.folder == {"slug": "roadmap", "created": True}
    with db.connect() as conn, conn.transaction():
        conn.execute("update folder set slug = 'plan'")
        conn.execute(
            "insert into folder_alias (owner, old_slug, folder_id)"
            " select owner, 'roadmap', id from folder"
        )
    again = store.publish(db, "alpha", "Q", PAGE, folder="roadmap")
    assert again.folder == {"slug": "plan", "created": False}, (
        "the old slug still works"
    )
    assert [r.title for r in store.list_in_folder(db, "alpha", "roadmap")] == ["Q", "P"]


def test_a_transfer_takes_the_page_out_of_the_old_owners_folder(db):
    keys.mint(db, agent="alpha")
    keys.mint(db, agent="beta")
    page = store.publish(db, "alpha", "P", PAGE, folder="roadmap")
    assert store.transfer(db, page.id, "beta", agent="alpha") == "alpha"
    assert store.get_owned(db, "beta", page.id).folder is None


def test_the_wall_is_unchanged_by_a_folder(client, tokens):
    page = publish(
        client, tokens["alpha"], title="Foldered page", folder="roadmap"
    ).json()
    wall = client.get("/").text
    assert page["id"] in wall, "slice 1: a foldered page is still on the wall"


# --- rename ---------------------------------------------------------------------


def _folders(client, token):
    got = client.get("/api/v1/folders", headers=auth(token)).json()
    return {f["slug"]: f["pages"] for f in got}


def test_renaming_an_agent_moves_its_folders_and_its_old_key_still_finds_them(
    client, db, tokens
):
    from artifacts import rename

    page = publish(client, tokens["alpha"], folder="roadmap").json()
    rename.rename_agent(db, "alpha", "gamma", dry_run=False)
    assert _folders(client, tokens["alpha"]) == {"roadmap": 1}  # old key, new name
    with db.connect() as conn:
        assert (
            conn.execute(
                "select count(*) from folder where owner = 'alpha'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("select owner from folder").fetchone()[0] == "gamma"
        assert (
            conn.execute(
                "select f.owner from artifact a join folder f on f.id = a.folder_id"
                " where a.id = %s",
                (page["id"],),
            ).fetchone()[0]
            == "gamma"
        )
    # A new agent given the old name inherits nothing.
    fresh = keys.mint(db, agent="alpha-new")[0]
    assert _folders(client, fresh) == {}


def test_a_rename_merge_with_a_clashing_slug_files_both_agents_pages_in_one_folder(
    client, db, tokens
):
    from artifacts import rename

    publish(client, tokens["alpha"], folder="roadmap")
    publish(client, tokens["alpha"], title="only-a", folder="solo")
    publish(client, tokens["beta"], folder="roadmap")
    with db.connect() as conn, conn.transaction():
        fid = conn.execute(
            "select id from folder where owner = 'alpha' and slug = 'roadmap'"
        ).fetchone()[0]
        conn.execute(
            "insert into folder_alias (owner, old_slug, folder_id) values ('alpha', 'rm', %s)",
            (fid,),
        )
    rename.rename_agent(db, "alpha", "beta", dry_run=False)
    assert _folders(client, tokens["beta"]) == {"roadmap": 2, "solo": 1}
    with db.connect() as conn:
        assert conn.execute("select count(*) from folder").fetchone()[0] == 2
        assert (
            conn.execute(
                "select f.slug from folder_alias a join folder f on f.id = a.folder_id"
                " where a.owner = 'beta' and a.old_slug = 'rm'"
            ).fetchone()[0]
            == "roadmap"
        )


def test_a_rename_never_touches_a_jeremy_folder(client, db, tokens):
    from artifacts import rename

    with db.connect() as conn, conn.transaction():
        conn.execute(
            "insert into folder (id, owner, slug, title, created_at)"
            " values ('f1', 'jeremy', 'mine', 'mine', %s)",
            (datetime.now(UTC),),
        )
    publish(client, tokens["alpha"], folder="x")
    rename.rename_agent(db, "alpha", "gamma", dry_run=False)
    with db.connect() as conn:
        assert (
            conn.execute("select owner from folder where id = 'f1'").fetchone()[0]
            == "jeremy"
        )


def test_folder_counts_ignore_a_page_another_publisher_put_in_an_agent_folder(
    client, db, tokens
):
    publish(client, tokens["alpha"], folder="roadmap")
    foreign = publish(client, tokens["beta"], title="theirs").json()
    one = publish(client, tokens["beta"], title="theirs2").json()
    with db.connect() as conn, conn.transaction():
        fid = conn.execute("select id from folder where owner = 'alpha'").fetchone()[0]
        for pid in (foreign["id"], one["id"]):
            conn.execute("update artifact set folder_id = %s where id = %s", (fid, pid))
    decisions.ask(
        db,
        agent="beta",
        artifact_id=foreign["id"],
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    got = client.get("/api/v1/folders", headers=auth(tokens["alpha"])).json()
    assert [(f["slug"], f["pages"], f["open_questions"]) for f in got] == [
        ("roadmap", 1, 0)
    ]
