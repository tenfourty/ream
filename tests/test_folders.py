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


def test_a_foldered_page_leaves_the_wall_for_its_folder_tile(client, tokens):
    page = publish(
        client, tokens["alpha"], title="Foldered page", folder="roadmap"
    ).json()
    wall = client.get("/").text
    assert f'href="/a/{page["id"]}"' not in wall, "the page is behind its folder tile"
    assert 'href="/f/' in wall


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


def test_a_form_is_not_an_open_question_in_the_folder_count(client, db, tokens):
    page = publish(client, tokens["alpha"], folder="roadmap").json()
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "insert into decision (id, artifact_id, version_n, question, options,"
            " kind, form_name) values ('df', %s, 1, 'q', '[]', 'form', 'f')",
            (page["id"],),
        )
    got = client.get("/api/v1/folders", headers=auth(tokens["alpha"])).json()
    assert [(f["slug"], f["pages"], f["open_questions"]) for f in got] == [
        ("roadmap", 1, 0)
    ]


# --- bulk move (slice 2) ------------------------------------------------------


def move(client, token, ids, folder):
    return client.post(
        "/api/v1/artifacts/move",
        json={"ids": ids, "folder": folder},
        headers=auth(token),
    )


def folder_of(client, token, page_id):
    rows = client.get("/api/v1/artifacts", headers=auth(token)).json()
    return next(r["folder"] for r in rows if r["id"] == page_id)


def test_a_move_into_a_new_folder_creates_it_and_says_so(client, tokens):
    one = publish(client, tokens["alpha"], title="one").json()["id"]
    two = publish(client, tokens["alpha"], title="two").json()["id"]
    got = move(client, tokens["alpha"], [one, two], "roadmap")
    assert got.status_code == 200
    assert got.json() == {
        "moved": [one, two],
        "missing": [],
        "folder": {"slug": "roadmap", "created": True},
    }
    assert folder_of(client, tokens["alpha"], one) == "roadmap"
    assert folder_of(client, tokens["alpha"], two) == "roadmap"


def test_a_move_into_an_existing_folder_says_created_false(client, tokens):
    publish(client, tokens["alpha"], folder="roadmap")
    one = publish(client, tokens["alpha"], title="one").json()["id"]
    got = move(client, tokens["alpha"], [one], "roadmap").json()
    assert got["folder"] == {"slug": "roadmap", "created": False}
    assert got["moved"] == [one]


def test_a_move_with_null_takes_pages_out_and_makes_no_version(client, tokens):
    one = publish(client, tokens["alpha"], folder="roadmap").json()
    got = move(client, tokens["alpha"], [one["id"]], None)
    assert got.json() == {"moved": [one["id"]], "missing": [], "folder": None}
    assert folder_of(client, tokens["alpha"], one["id"]) is None
    versions = client.get(
        f"/api/v1/artifacts/{one['id']}/versions", headers=auth(tokens["alpha"])
    ).json()
    assert len(versions) == 1, "filing is a shelf fact"


def test_another_agents_id_and_an_unknown_id_are_both_missing(client, tokens):
    mine = publish(client, tokens["alpha"], title="mine").json()["id"]
    theirs = publish(client, tokens["beta"], title="theirs").json()["id"]
    got = move(client, tokens["alpha"], [mine, theirs, "nonesuch"], "roadmap").json()
    assert got["moved"] == [mine]
    assert got["missing"] == [theirs, "nonesuch"], "not yours reads as no such page"
    assert folder_of(client, tokens["beta"], theirs) is None


def test_all_missing_creates_no_folder_and_changes_nothing(client, tokens):
    theirs = publish(client, tokens["beta"]).json()["id"]
    got = move(client, tokens["alpha"], [theirs, "nonesuch"], "roadmap").json()
    assert got == {"moved": [], "missing": [theirs, "nonesuch"], "folder": None}
    assert client.get("/api/v1/folders", headers=auth(tokens["alpha"])).json() == []


def test_a_binned_page_is_missing_and_a_repeated_id_moves_once(client, db, tokens):
    one = publish(client, tokens["alpha"], title="one").json()["id"]
    two = publish(client, tokens["alpha"], title="two").json()["id"]
    assert store.set_deleted(db, two, True, agent="alpha")
    got = move(client, tokens["alpha"], [one, one, two], "roadmap").json()
    assert got["moved"] == [one]
    assert got["missing"] == [two]


def test_more_than_200_ids_is_a_422_and_an_empty_list_too(client, tokens):
    over = [f"x{i}" for i in range(201)]
    assert move(client, tokens["alpha"], over, "a").status_code == 422
    assert move(client, tokens["alpha"], [], "a").status_code == 422
    assert move(client, tokens["alpha"], over[:200], "a").status_code == 200


def test_a_move_needs_the_folder_field_and_a_good_slug_and_a_key(client, tokens):
    one = publish(client, tokens["alpha"]).json()["id"]
    bare = client.post(
        "/api/v1/artifacts/move", json={"ids": [one]}, headers=auth(tokens["alpha"])
    )
    assert bare.status_code == 422
    bad = move(client, tokens["alpha"], [one], "Road Map")
    assert bad.status_code == 422
    assert "folder" in str(bad.json())
    assert folder_of(client, tokens["alpha"], one) is None
    nokey = client.post("/api/v1/artifacts/move", json={"ids": [one], "folder": None})
    assert nokey.status_code in (401, 403)


def test_a_move_into_an_archived_folder_is_a_409_and_changes_nothing(
    client, db, tokens
):
    publish(client, tokens["alpha"], folder="roadmap")
    one = publish(client, tokens["alpha"], title="one").json()["id"]
    with db.connect() as conn:
        conn.execute("update folder set archived_at = %s", (datetime.now(UTC),))
    got = move(client, tokens["alpha"], [one], "roadmap")
    assert got.status_code == 409
    assert folder_of(client, tokens["alpha"], one) is None


# --- slice 2: rename, aliases, archive ----------------------------------------------


def _ask(db, agent, page_id, question="Which?"):
    return decisions.ask(
        db,
        agent=agent,
        artifact_id=page_id,
        question=question,
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )


def post(client, token, path, **body):
    return client.post(f"/api/v1{path}", json=body, headers=auth(token))


def rename_folder(client, token, old, **body):
    return post(client, token, f"/folders/{old}/rename", **body)


def archive_folder(client, token, slug, archived=True, **extra):
    return post(client, token, f"/folders/{slug}/archive", archived=archived, **extra)


def page_archived(db, page_id):
    with db.connect() as conn:
        return conn.execute(
            "select archived_at, archived_via_folder from artifact where id = %s",
            (page_id,),
        ).fetchone()


def kinds(db, agent):
    from artifacts import events

    return [r.kind for r in events.list_for_agent(db, agent, since=0, limit=100)]


def test_rename_changes_the_slug_and_the_old_slug_still_resolves_everywhere(
    client, tokens
):
    t = tokens["alpha"]
    one = publish(client, t, folder="roadmap").json()["id"]
    got = rename_folder(client, t, "roadmap", slug="plan")
    assert got.status_code == 200
    assert got.json() == {
        "slug": "plan",
        "title": "roadmap",
        "previous_slug": "roadmap",
    }
    assert set(_folders(client, t)) == {"plan"}
    # list route, publish and bulk move all take the old slug and make nothing new
    listed = client.get("/api/v1/folders/roadmap/artifacts", headers=auth(t))
    assert [r["id"] for r in listed.json()] == [one]
    again = publish(client, t, title="Q", folder="roadmap").json()
    assert again["folder"] == {"slug": "plan", "created": False}
    other = publish(client, t, title="loose").json()["id"]
    moved = move(client, t, [other], "roadmap").json()
    assert moved["folder"] == {"slug": "plan", "created": False}
    assert set(_folders(client, t)) == {"plan"}
    # the old slug renames too
    assert rename_folder(client, t, "roadmap", title="Plan").json()["title"] == "Plan"


def test_a_title_only_rename_keeps_the_slug_and_makes_no_alias(client, db, tokens):
    t = tokens["alpha"]
    publish(client, t, folder="cps")
    got = rename_folder(client, t, "cps", title="Cove public split")
    assert got.json() == {
        "slug": "cps",
        "title": "Cove public split",
        "previous_slug": None,
    }
    (row,) = client.get("/api/v1/folders", headers=auth(t)).json()
    assert row["title"] == "Cove public split"
    with db.connect() as conn:
        assert conn.execute("select count(*) from folder_alias").fetchone()[0] == 0


def test_rename_to_a_used_slug_or_another_folders_old_slug_is_409(client, tokens):
    t = tokens["alpha"]
    publish(client, t, folder="a")
    publish(client, t, folder="b")
    assert rename_folder(client, t, "a", slug="b").status_code == 409
    rename_folder(client, t, "b", slug="c")  # b is now an alias of c
    clash = rename_folder(client, t, "a", slug="b")
    assert clash.status_code == 409 and clash.json()["detail"]["field"] == "slug"
    assert set(_folders(client, t)) == {"a", "c"}, "the refusals changed nothing"
    # another agent's same names are no clash
    publish(client, tokens["beta"], folder="x")
    assert rename_folder(client, tokens["beta"], "x", slug="b").status_code == 200


def test_renaming_back_to_its_own_old_slug_and_to_itself_is_fine(client, tokens):
    t = tokens["alpha"]
    publish(client, t, folder="a")
    rename_folder(client, t, "a", slug="b")
    assert rename_folder(client, t, "b", slug="b").json()["previous_slug"] is None
    back = rename_folder(client, t, "b", slug="a")
    assert back.status_code == 200 and back.json()["slug"] == "a"
    assert set(_folders(client, t)) == {"a"}
    assert publish(client, t, folder="b").json()["folder"] == {
        "slug": "a",
        "created": False,
    }, "b is still an old name of the folder"


def test_rename_needs_something_and_a_good_slug_and_scans_the_title(client, tokens):
    t = tokens["alpha"]
    publish(client, t, folder="a")
    assert rename_folder(client, t, "a").status_code == 422
    assert rename_folder(client, t, "a", slug="Bad Slug").status_code == 422
    assert rename_folder(client, t, "a", slug="").status_code == 422
    secret = rename_folder(client, t, "a", title="AKIA" + "IOSFODNN7EXAMPLE")
    assert secret.status_code >= 400
    assert set(_folders(client, t)) == {"a"}


def test_read_and_rename_routes_never_create_and_other_agents_get_404(client, tokens):
    t = tokens["alpha"]
    assert rename_folder(client, t, "ghost", title="x").status_code == 404
    assert archive_folder(client, t, "ghost").status_code == 404
    listing = client.get("/api/v1/folders/ghost/artifacts", headers=auth(t))
    assert listing.status_code == 404
    assert _folders(client, t) == {}
    publish(client, tokens["beta"], folder="theirs")
    for got in (
        rename_folder(client, t, "theirs", title="mine"),
        archive_folder(client, t, "theirs", confirm=True),
        archive_folder(client, t, "theirs", archived=False),
    ):
        assert got.status_code == 404
    assert _folders(client, t) == {}
    assert _folders(client, tokens["beta"]) == {"theirs": 1}
    ghost = rename_folder(client, t, "ghost", title="x")
    theirs = rename_folder(client, t, "theirs", title="x")
    assert ghost.json() == theirs.json(), "identical answers"


def test_archive_without_confirm_changes_nothing_and_counts(client, db, tokens):
    t = tokens["alpha"]
    one = publish(client, t, folder="f").json()["id"]
    two = publish(client, t, title="two", folder="f").json()["id"]
    _ask(db, "alpha", one)
    _ask(db, "alpha", one, "Also?")
    _ask(db, "alpha", two)
    got = archive_folder(client, t, "f")
    assert got.status_code == 200
    assert got.json()["would_archive"] == {"pages": 2, "questions_set_aside": 3}
    assert page_archived(db, one) == (None, None)
    assert _folders(client, t) == {"f": 2}
    assert client.get("/api/v1/folders", headers=auth(t)).json()[0]["archived"] is False
    assert "decision_set_aside" not in kinds(db, "alpha")
    # `archived` is required
    assert post(client, t, "/folders/f/archive").status_code == 422


def test_archive_confirm_archives_pages_sets_questions_aside_and_tells_the_agent(
    client, db, tokens
):
    t = tokens["alpha"]
    one = publish(client, t, folder="f").json()["id"]
    two = publish(client, t, title="two", folder="f").json()["id"]
    outside = publish(client, t, title="loose").json()["id"]
    _ask(db, "alpha", one)
    _ask(db, "alpha", two)
    _ask(db, "alpha", outside)
    got = archive_folder(client, t, "f", confirm=True).json()
    assert got == {"slug": "f", "archived": True, "pages": 2, "questions_set_aside": 2}
    with db.connect() as conn:
        fid = conn.execute("select id from folder").fetchone()[0]
    for pid in (one, two):
        at, via = page_archived(db, pid)
        assert at is not None and via == fid
    assert page_archived(db, outside) == (None, None)
    assert kinds(db, "alpha").count("decision_set_aside") == 2
    (row,) = client.get("/api/v1/folders", headers=auth(t)).json()
    assert row["archived"] is True and row["open_questions"] == 0
    # it is an archived folder now: publish and move refuse
    assert publish(client, t, folder="f").status_code == 409
    assert move(client, t, [outside], "f").status_code == 409


def test_unarchive_restores_only_its_pages_and_reopens_their_questions(
    client, db, tokens
):
    t = tokens["alpha"]
    one = publish(client, t, folder="f").json()["id"]
    mine = publish(client, t, title="by hand", folder="f").json()["id"]
    _ask(db, "alpha", one)
    _ask(db, "alpha", mine)
    post(client, t, f"/artifacts/{mine}/archive", archived=True)  # before the folder
    got = archive_folder(client, t, "f", confirm=True).json()
    assert got["pages"] == 1 and got["questions_set_aside"] == 1, (
        "only the page that was live: the other's question was set aside long ago"
    )
    assert page_archived(db, mine)[1] is None
    back = archive_folder(client, t, "f", archived=False).json()
    assert back == {
        "slug": "f",
        "archived": False,
        "pages_restored": 1,
        "questions_reopened": 1,
    }
    assert page_archived(db, one) == (None, None)
    assert page_archived(db, mine)[0] is not None, "his own archive survives"
    assert kinds(db, "alpha").count("decision_reopened") == 1
    (row,) = client.get("/api/v1/folders", headers=auth(t)).json()
    assert row["archived"] is False and row["open_questions"] == 1
    assert publish(client, t, title="new", folder="f").status_code == 201


def test_page_level_archive_unarchive_or_a_move_out_clears_the_marker(
    client, db, tokens
):
    t = tokens["alpha"]
    a, b, c = (
        publish(client, t, title=n, folder="f").json()["id"] for n in ("a", "b", "c")
    )
    archive_folder(client, t, "f", confirm=True)
    post(client, t, f"/artifacts/{a}/archive", archived=False)  # unarchive by itself
    post(client, t, f"/artifacts/{b}/archive", archived=True)  # archive again by itself
    assert page_archived(db, a) == (None, None)
    assert page_archived(db, b)[1] is None and page_archived(db, b)[0] is not None
    assert move(client, t, [c], None).status_code == 200  # out, still archived
    assert page_archived(db, c)[0] is not None and page_archived(db, c)[1] is None
    back = archive_folder(client, t, "f", archived=False).json()
    assert back["pages_restored"] == 0
    assert page_archived(db, b)[0] is not None
    assert page_archived(db, c)[0] is not None


def test_a_patch_out_of_the_folder_and_a_transfer_clear_the_marker(client, db, tokens):
    t = tokens["alpha"]
    a = publish(client, t, title="a", folder="f").json()
    b = publish(client, t, title="b", folder="f").json()["id"]
    archive_folder(client, t, "f", confirm=True)
    store.transfer(db, b, "beta", agent="alpha")
    assert page_archived(db, b)[1] is None and page_archived(db, b)[0] is not None
    got = _patch(client, t, a["id"], a["revision"], folder="")
    assert got.status_code == 200
    assert page_archived(db, a["id"])[1] is None
    assert page_archived(db, a["id"])[0] is not None


def test_a_folder_is_archived_and_refuses_through_the_old_slug_after_a_rename(
    client, db, tokens
):
    t = tokens["alpha"]
    one = publish(client, t, folder="old").json()["id"]
    rename_folder(client, t, "old", slug="new")
    assert archive_folder(client, t, "old", confirm=True).json()["slug"] == "new"
    assert page_archived(db, one)[0] is not None
    assert publish(client, t, folder="old").status_code == 409
    assert move(client, t, [one], "old").status_code == 409
    assert archive_folder(client, t, "old", archived=False).status_code == 200
    assert page_archived(db, one)[0] is None


def test_a_binned_page_is_left_out_of_an_archive(client, db, tokens):
    t = tokens["alpha"]
    one = publish(client, t, folder="f").json()["id"]
    binned = publish(client, t, title="bin", folder="f").json()["id"]
    store.set_deleted(db, binned, True, agent="alpha")
    assert archive_folder(client, t, "f", confirm=True).json()["pages"] == 1
    assert page_archived(db, binned) == (None, None)
    assert page_archived(db, one)[0] is not None


def test_archiving_twice_is_harmless(client, tokens):
    t = tokens["alpha"]
    publish(client, t, folder="f")
    assert archive_folder(client, t, "f", confirm=True).json()["pages"] == 1
    again = archive_folder(client, t, "f", confirm=True).json()
    assert again["pages"] == 0 and again["questions_set_aside"] == 0
    assert archive_folder(client, t, "f", archived=False).json()["pages_restored"] == 1


def test_move_with_a_folder_title_names_a_new_folder_only(client, tokens):
    t = tokens["alpha"]
    one = publish(client, t, title="one").json()["id"]
    two = publish(client, t, title="two").json()["id"]
    got = client.post(
        "/api/v1/artifacts/move",
        json={"ids": [one], "folder": "cps", "folder_title": "Cove public split"},
        headers=auth(t),
    )
    assert got.json()["folder"] == {"slug": "cps", "created": True}
    client.post(
        "/api/v1/artifacts/move",
        json={"ids": [two], "folder": "cps", "folder_title": "Ignored"},
        headers=auth(t),
    )
    (row,) = client.get("/api/v1/folders", headers=auth(t)).json()
    assert row["title"] == "Cove public split"


def test_agent_rename_merge_keeps_an_archived_survivor_and_carries_aliases(
    client, db, tokens
):
    from artifacts import rename

    a, b = tokens["alpha"], tokens["beta"]
    publish(client, a, title="p", folder="solo")
    rename_folder(client, a, "solo", slug="solo2")  # alias solo -> solo2
    publish(client, a, title="x", folder="shared")
    publish(client, b, title="y", folder="shared")
    rename_folder(client, a, "shared", slug="mine")  # alpha alias `shared`
    publish(client, a, title="clash", folder="clash")
    publish(client, b, title="clash", folder="clash")
    archive_folder(client, b, "clash", confirm=True)
    rename.rename_agent(db, "alpha", "beta", dry_run=False)
    with db.connect() as conn:
        rows = dict(
            conn.execute("select slug, archived_at is not null from folder").fetchall()
        )
        assert rows == {
            "solo2": False,
            "shared": False,
            "mine": False,
            "clash": True,
        }, "the archived survivor stays archived"
        survivor = conn.execute(
            "select id from folder where slug = 'clash'"
        ).fetchone()[0]
        via = conn.execute(
            "select archived_via_folder from artifact"
            " where archived_via_folder is not null"
        ).fetchall()
        assert via == [(survivor,)], "the survivor's own archived page keeps its marker"
        aliases = dict(conn.execute("select old_slug, folder_id from folder_alias"))
        assert "solo" in aliases, "an old alias moves over"
        assert "shared" not in aliases, "an alias shadowed by a real slug is dropped"
    got = client.get("/api/v1/folders/shared/artifacts", headers=auth(b)).json()
    assert [r["title"] for r in got] == ["y"], "the real folder wins"
    old = client.get("/api/v1/folders/solo/artifacts", headers=auth(b)).json()
    assert [r["title"] for r in old] == ["p"]


def test_agent_rename_merge_of_an_archived_old_folder_into_a_live_one(
    client, db, tokens
):
    from artifacts import rename

    a, b = tokens["alpha"], tokens["beta"]
    publish(client, b, title="live", folder="clash")
    old = publish(client, a, title="old", folder="clash").json()["id"]
    archive_folder(client, a, "clash", confirm=True)
    rename.rename_agent(db, "alpha", "beta", dry_run=False)
    at, via = page_archived(db, old)
    assert at is not None, "still archived"
    assert via is None, "on its own now: the live folder's archive never touches it"
    assert archive_folder(client, b, "clash", confirm=True).json()["pages"] == 1
    back = archive_folder(client, b, "clash", archived=False).json()
    assert back["pages_restored"] == 1
    assert page_archived(db, old)[0] is not None


def test_an_oversize_folder_title_is_a_413_on_move_and_rename(client, tokens):
    t = tokens["alpha"]
    one = publish(client, t, folder="f").json()["id"]
    big = "x" * 600
    got = rename_folder(client, t, "f", title=big)
    assert got.status_code == 413
    assert got.json()["detail"]["field"] == "title"
    assert got.json()["detail"]["size"] == 600
    moved = client.post(
        "/api/v1/artifacts/move",
        json={"ids": [one], "folder": "g", "folder_title": big},
        headers=auth(t),
    )
    assert moved.status_code == 413
    assert moved.json()["detail"]["field"] == "folder_title"
    assert set(_folders(client, t)) == {"f"}, "nothing was created or renamed"
