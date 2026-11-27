"""Personal machines (agents design, slice 4, docs/agent/specs/2026-11-26-agents-and-watching-design.md):
a key's agent the owner marked as one person's (`agent_person`) READS as that person does and still
CHANGES only its own pages (decision D). A shared key is exactly as before; "not yours" answers
identically to "no such page"; the person is set only by `artifacts-admin` and the owner's Machines
page, never by an API or MCP route. Both databases (the `db` fixture)."""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ream import (
    agents,
    events,
    keys,
    mcp,
    owner,
    people,
    reach,
    settings,
    store,
    watches,
)
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"
MISSING = "abcdefghijklmnopqrstuvwxyz"


@pytest.fixture(autouse=True)
def _sign_in_open(monkeypatch):
    monkeypatch.setattr(people, "OPEN_SIGN_IN", True)


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db), base_url="https://testserver")


def _person(db, pid):
    with db.connect() as conn:
        conn.execute("insert into person (id) values (%s)", (pid,))


def _closed(db, agent, title):
    page = store.publish(db, agent, title, PAGE.format(title))
    store.set_agents_can_read(db, page.id, False, agent=agent)
    return page.id


@pytest.fixture()
def world(db):
    owner.check(db, settings.owner_id())  # seeds person #1, as a started service has
    _person(db, "ann")
    heads = {}
    for name in ("owner-box", "ann-box", "ann-box2", "office"):
        token, _ = keys.mint(db, agent=name)
        heads[name] = {"Authorization": f"Bearer {token}"}
    owner_id = settings.owner_id()
    return {
        "heads": heads,
        "ann_closed": _closed(db, "ann-box2", "Ann closed"),
        "office_closed": _closed(db, "office", "Office closed"),
        "office_open": store.publish(db, "office", "Open", PAGE.format("o")).id,
        "own_closed": _closed(db, "ann-box", "Ann own closed"),
        "owner": owner_id,
    }


def _get(api, who, page_id):
    return api.get(f"/api/v1/pages/{page_id}", headers=who)


def test_a_shared_key_reads_as_it_always_did(api, world):
    h = world["heads"]["ann-box"]
    assert _get(api, h, world["office_open"]).status_code == 200
    assert _get(api, h, world["office_closed"]).status_code == 404
    assert (
        _get(api, h, world["ann_closed"]).status_code == 404
    )  # not yet the same person
    assert "belongs_to" not in api.get("/api/v1/me", headers=h).json()


def test_a_personal_key_reads_its_persons_pages_never_the_owners_closed_one(
    db, api, world
):
    agents.set_person(db, "ann-box", "ann", "admin")
    agents.set_person(db, "ann-box2", "ann", "admin")
    h = world["heads"]["ann-box"]
    for page in ("ann_closed", "own_closed", "office_open"):
        assert _get(api, h, world[page]).status_code == 200, page
    gone = _get(api, h, world["office_closed"])
    assert gone.status_code == 404
    assert gone.json() == _get(api, h, MISSING).json()  # the 404 a missing page gets
    listed = {r["id"] for r in api.get("/api/v1/pages?scope=all", headers=h).json()}
    assert world["ann_closed"] in listed and world["office_closed"] not in listed
    # Another person's shared key still does not read it.
    assert (
        _get(api, world["heads"]["owner-box"], world["ann_closed"]).status_code == 404
    )


def test_the_owners_personal_key_reads_every_live_page_and_not_the_trash(
    db, api, world
):
    agents.set_person(db, "owner-box", world["owner"], "admin")
    h = world["heads"]["owner-box"]
    assert _get(api, h, world["office_closed"]).status_code == 200
    assert _get(api, h, world["ann_closed"]).status_code == 200
    gone = store.publish(db, "office", "Trashed", PAGE.format("t")).id
    store.set_deleted(db, gone, True, agent="office")
    assert _get(api, h, gone).status_code == 404


def test_writes_stay_on_its_own_pages(db, api, world):
    agents.set_person(db, "owner-box", world["owner"], "admin")
    mine, shared = world["heads"]["owner-box"], world["heads"]["ann-box"]
    bodies = {
        "archive": {"archived": True},
        "rename": {"title": "x"},
        "pin": {"pinned": True},
    }
    for path, body in bodies.items():
        url = f"/api/v1/pages/{world['office_open']}/{path}"
        a = api.post(url, headers=mine, json=body)
        b = api.post(url, headers=shared, json=body)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), path
        assert a.status_code == 404, path
    # and the page is untouched
    assert store.get_owned(db, "office", world["office_open"]).title == "Open"


def test_me_says_whose_it_is_read_only_and_only_for_a_personal_key(db, api, world):
    h = world["heads"]["ann-box"]
    before = api.get("/api/v1/me", headers=h).json()
    agents.set_person(db, "ann-box", "ann", "admin")
    after = api.get("/api/v1/me", headers=h).json()
    assert after["belongs_to"] == "ann" and after["shown_as"] == "ann, via ann-box"
    assert {
        k: v for k, v in after.items() if k not in ("belongs_to", "shown_as")
    } == before
    agents.clear_person(db, "ann-box")
    assert api.get("/api/v1/me", headers=h).json() == before


def test_clearing_narrows_back_at_once(db, api, world):
    agents.set_person(db, "ann-box", "ann", "admin")
    agents.set_person(db, "ann-box2", "ann", "admin")
    h = world["heads"]["ann-box"]
    assert _get(api, h, world["ann_closed"]).status_code == 200
    agents.clear_person(db, "ann-box")
    assert _get(api, h, world["ann_closed"]).status_code == 404


def test_set_person_refusals(db, world):
    with pytest.raises(agents.Refused, match="holds a live key"):
        agents.set_person(db, "nobody", "ann", "admin")
    with pytest.raises(agents.Refused, match="no person"):
        agents.set_person(db, "ann-box", "zed", "admin")
    assert agents.set_person(db, "ann-box", "ann", "admin") is True
    assert agents.set_person(db, "ann-box", "ann", "admin") is False
    assert agents.clear_person(db, "ann-box") is True
    assert agents.clear_person(db, "ann-box") is False


def test_a_personal_agents_questions_go_to_its_person(db, world):
    assert agents.page_owners(db.connect().__enter__(), "ann-box") == (world["owner"],)
    agents.set_person(db, "ann-box", "ann", "admin")
    with db.connect() as conn:
        assert agents.person_of(conn, "ann-box") == "ann"
        assert agents.page_owners(conn, "ann-box") == ("ann",)
        assert agents.key_reach(conn, "ann-box").reads_person()
        assert not agents.key_reach(conn, "ann-box").writes_others()
        assert agents.key_reach(conn, "office") == reach.of_key("office")


def test_mcp_key_calls_get_the_same_reach(db, world):
    agents.set_person(db, "ann-box", "ann", "admin")
    assert mcp._key_reach(db, "ann-box") == agents.key_reach(
        db.connect().__enter__(), "ann-box"
    )
    assert mcp._key_reach(db, "office") == reach.of_key("office")


def test_watching_uses_the_widened_predicate(db, api, world):
    """A personal key watches a page its person reads; a change reaches it while it may read the
    page and goes silent once the key is shared again."""
    agents.set_person(db, "owner-box", world["owner"], "admin")
    h = world["heads"]["owner-box"]
    page = world["office_closed"]
    assert api.put(f"/api/v1/pages/{page}/watch", headers=h).status_code == 200

    def edit(text):
        row = store.get_owned(db, "office", page)
        store.patch(db, "office", page, PAGE.format(text), row.revision)

    def heard():
        return [
            e
            for e in events.list_for_agent(db, "owner-box", include_watched=True)
            if e.kind == "watched_page_changed"
        ]

    edit("two")
    assert len(heard()) == 1
    agents.clear_person(db, "owner-box")
    with db.connect() as conn:
        clause, params = watches._reads(conn, "owner-box")
    assert "agents_can_read" in clause and params == ("owner-box",)
    # window: a second change inside it is folded anyway, so check the clause, not the count
    assert watches.listing(db, "owner-box") == []


# --- no API or MCP route takes or sets an agent's person -----------------------------------


def test_no_api_route_takes_an_agents_person(db):
    """Modelled on `test_no_gallery_route_takes_a_person_id_from_the_request`: no parameter of any
    /api/v1 route is named like a person or a belonging."""
    from fastapi.routing import APIRoute

    from ream.api import agent_router

    routes = [r for r in agent_router(db).routes if isinstance(r, APIRoute)]
    assert len(routes) > 40, "the walk is broken"
    named = set()
    for route in routes:
        stack = [route.dependant]
        while stack:
            d = stack.pop()
            stack.extend(d.dependencies)
            for param in (
                d.path_params
                + d.query_params
                + d.header_params
                + d.cookie_params
                + d.body_params
            ):
                if re.search(r"person|belongs|actor|as_user|on_behalf", param.name):
                    named.add((route.path, param.name))
    assert named == set(), named


def test_no_mcp_tool_takes_an_agents_person():
    from ream import mcp_tools  # noqa: F401 -- registers the tools

    assert len(mcp.TOOLS) > 15, "the walk is broken"
    named = {
        (name, prop)
        for name, t in mcp.TOOLS.items()
        for prop in t.input_schema.get("properties", {})
        if re.search(r"person|belongs|actor|as_user|on_behalf", prop)
    }
    assert named == set(), named


def test_only_the_owners_surfaces_write_agent_person():
    """The table is written by `agents.set_person`/`clear_person` alone, and those are called from
    `admin.py` and the gallery's owner-only Machines route and from nothing else."""
    src = Path(__file__).resolve().parent.parent / "src" / "ream"
    writers, callers = set(), set()
    for path in src.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.search(
                    r"(insert into|update|delete from)\s+agent_person", node.value
                )
            ):
                writers.add(path.name)
            if isinstance(node, ast.Attribute) and node.attr in (
                "set_person",
                "clear_person",
            ):
                callers.add(path.name)
    assert writers <= {"agents.py", "keys.py", "rename.py", "owner.py", "db.py"}, (
        writers
    )
    assert callers == {"admin.py", "gallery.py"}, callers


def test_the_machines_route_is_the_owners_and_takes_no_person_parameter(db):
    from fastapi.routing import APIRoute

    from ream.gallery import gallery_router

    route = next(
        r
        for r in gallery_router(db).routes
        if isinstance(r, APIRoute) and r.path == "/machines/{key_id}/belongs"
    )

    def takes(d, call):
        return any(x.call is call or takes(x, call) for x in d.dependencies)

    assert takes(route.dependant, people.require_owner)
    assert takes(route.dependant, people.current)


def test_the_owner_marks_a_machine_personal_on_the_machines_page(db, world):
    """The owner's page (the suite's default request is the owner's): a form per machine, a post
    that sets the person, refuses a stranger id with a note, and clears it again."""
    page = TestClient(create_gallery_app(db))
    same = {"sec-fetch-site": "same-origin"}
    _t, key_id = keys.mint(db, agent="laptop")
    assert f'action="/machines/{key_id}/belongs"' in page.get("/machines").text
    done = page.post(
        f"/machines/{key_id}/belongs", data={"belongs_to": "ann"}, headers=same
    )
    assert done.status_code == 200 and "now belongs to ann" in done.text
    with db.connect() as conn:
        assert agents.person_of(conn, "laptop") == "ann"
    bad = page.post(
        f"/machines/{key_id}/belongs", data={"belongs_to": "nobody"}, headers=same
    )
    assert "Not changed" in bad.text
    page.post(f"/machines/{key_id}/belongs", data={"belongs_to": ""}, headers=same)
    with db.connect() as conn:
        assert agents.person_of(conn, "laptop") is None
    assert (
        page.post(
            "/machines/nope/belongs", data={"belongs_to": "ann"}, headers=same
        ).status_code
        == 404
    )


def _writes(pid, ids):
    """Every change a key can aim at one page, as (name, method, url, json body)."""
    return [
        (
            "patch",
            "patch",
            f"/api/v1/pages/{pid}",
            {"body": PAGE.format("x"), "expected_revision": "r1"},
        ),
        ("delete", "delete", f"/api/v1/pages/{pid}", None),
        ("transfer", "post", f"/api/v1/pages/{pid}/transfer", {"to": "owner-box"}),
        ("agents", "post", f"/api/v1/pages/{pid}/agents", {"can_read": True}),
        ("move", "post", "/api/v1/pages/move", {"ids": [pid], "folder": "mine"}),
        (
            "ask",
            "post",
            f"/api/v1/pages/{pid}/decisions",
            {
                "question": "q?",
                "options": [
                    {"letter": "a", "label": "A"},
                    {"letter": "b", "label": "B"},
                ],
            },
        ),
    ]


@pytest.mark.parametrize("page", ["office_closed", "office_open"])
def test_a_personal_key_changes_nothing_on_another_agents_page(db, api, world, page):
    """Reads widen, writes do not: on another agent's page, closed to agents or open, each change
    answers as it does for a shared key, and on a closed page as for a missing one."""
    agents.set_person(db, "owner-box", world["owner"], "admin")
    mine, shared = world["heads"]["owner-box"], world["heads"]["ann-box"]
    pid = world[page]
    for name, method, url, body in _writes(pid, None):
        a = api.request(method, url, headers=mine, json=body)
        b = api.request(method, url, headers=shared, json=body)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), name
        if name == "move":  # a 200 that moves nothing: "missing" covers "not yours"
            assert a.json()["moved"] == [] and pid in a.json()["missing"]
        else:
            assert a.status_code in (403, 404, 409), (name, a.status_code)
        if page == "office_closed":
            _n, m_method, m_url, m_body = next(
                w for w in _writes(MISSING, None) if w[0] == name
            )
            gone = api.request(m_method, m_url, headers=mine, json=m_body)
            assert a.status_code == gone.status_code, name
    row = store.get_owned(db, "office", pid)
    assert row.title in ("Open", "Office closed")
    assert row.deleted_at is None and row.agent == "office"


def test_a_personal_key_may_comment_where_it_may_read(db, api, world):
    agents.set_person(db, "ann-box", "ann", "admin")
    agents.set_person(db, "ann-box2", "ann", "admin")
    h = world["heads"]["ann-box"]
    ok = api.post(
        f"/api/v1/pages/{world['ann_closed']}/comments",
        headers=h,
        json={"body": "hello"},
    )
    assert ok.status_code == 201, ok.text
    no = api.post(
        f"/api/v1/pages/{world['office_closed']}/comments",
        headers=h,
        json={"body": "hello"},
    )
    assert no.status_code == 404


def _live(db, agent):
    with db.connect() as conn:
        return conn.execute(
            "select count(*) from agent_key where agent = %s and revoked_at is null",
            (agent,),
        ).fetchone()[0]


def test_revoking_the_last_key_drops_the_personal_mark(db, world):
    _t, key_id = keys.mint(db, agent="laptop")
    agents.set_person(db, "laptop", "ann", "admin")
    assert keys.revoke(db, key_id, actor="admin") == 1
    with db.connect() as conn:
        assert agents.person_of(conn, "laptop") is None
    # A machine connected under that name afterwards is a shared key.
    keys.mint(db, agent="laptop")
    with db.connect() as conn:
        assert agents.key_reach(conn, "laptop") == reach.of_key("laptop")
        assert agents.shown(conn, "laptop").text() == "laptop"


def test_revoking_one_of_two_live_keys_keeps_the_mark(db, world):
    _t1, first = keys.mint(db, agent="laptop")
    keys.mint(db, agent="laptop")
    agents.set_person(db, "laptop", "ann", "admin")
    assert keys.revoke(db, first, actor="admin") == 1
    assert _live(db, "laptop") == 1
    with db.connect() as conn:
        assert agents.person_of(conn, "laptop") == "ann"


def test_a_revoked_only_agent_cannot_be_marked(db, world):
    _t, key_id = keys.mint(db, agent="laptop")
    keys.revoke(db, key_id, actor="admin")
    with pytest.raises(agents.Refused, match="live key"):
        agents.set_person(db, "laptop", "ann", "admin")
    page = TestClient(create_gallery_app(db))
    same = {"sec-fetch-site": "same-origin"}
    refused = page.post(
        f"/machines/{key_id}/belongs", data={"belongs_to": "ann"}, headers=same
    )
    missing = page.post(
        "/machines/nope/belongs", data={"belongs_to": "ann"}, headers=same
    )
    assert refused.status_code == missing.status_code == 404
    with db.connect() as conn:
        assert agents.person_of(conn, "laptop") is None


def test_a_cross_origin_post_to_belongs_is_refused(db, world):
    _t, key_id = keys.mint(db, agent="laptop")
    page = TestClient(create_gallery_app(db))
    for hdr in ({"sec-fetch-site": "cross-site"}, {"origin": "https://evil.example"}):
        r = page.post(
            f"/machines/{key_id}/belongs", data={"belongs_to": "ann"}, headers=hdr
        )
        assert r.status_code in (400, 403), (hdr, r.status_code)
    with db.connect() as conn:
        assert agents.person_of(conn, "laptop") is None


def _ask_connect(gallery, name):
    r = gallery.post(
        "/api/v1/connect", json={"name": name}, headers={"x-real-ip": "203.0.113.5"}
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_the_approve_page_says_a_marked_agent_belongs_to_a_person(db, world):
    """The "same agent, new machine" choice names the person a marked agent belongs to (escaped),
    so the owner knows the new machine will read what they read; an unmarked agent gets no line."""
    with db.connect() as conn:
        conn.execute("update person set name = %s where id = 'ann'", ("<b>Ann</b>",))
    keys.mint(db, agent="laptop")
    keys.mint(db, agent="shared-box")
    agents.set_person(db, "laptop", "ann", "admin")
    gallery = TestClient(create_gallery_app(db), base_url="https://testserver")
    marked = gallery.get(
        "/connect", params={"code": _ask_connect(gallery, "laptop")["code"]}
    ).text
    assert "Same agent, new machine" in marked
    assert (
        "laptop belongs to &lt;b&gt;Ann&lt;/b&gt;: this machine will read what "
        "&lt;b&gt;Ann&lt;/b&gt; reads." in marked
    )
    assert "<b>Ann</b>" not in marked and "belongs-note" in marked
    plain = gallery.get(
        "/connect", params={"code": _ask_connect(gallery, "shared-box")["code"]}
    ).text
    assert "Same agent, new machine" in plain
    assert "belongs-note" not in plain and "will read what" not in plain


def test_the_approve_page_keeps_the_line_when_it_redraws_after_a_refused_name(
    db, world
):
    keys.mint(db, agent="laptop")
    agents.set_person(db, "laptop", "ann", "admin")
    gallery = TestClient(create_gallery_app(db), base_url="https://testserver")
    code = _ask_connect(gallery, "laptop")["code"]
    page = gallery.get("/connect", params={"code": code}).text
    request_id = re.search(r'name="request_id" value="([^"]+)"', page).group(1)
    again = gallery.post(
        "/connect/approve",
        data={
            "request_id": request_id,
            "code": code,
            "name": "laptop",
            "choice": "new",
        },
        headers={"sec-fetch-site": "same-origin"},
    ).text
    assert "laptop belongs to ann: this machine will read what ann reads." in again


def test_a_new_key_approved_onto_a_marked_agent_inherits_the_mark(db, world):
    keys.mint(db, agent="laptop")  # the existing machine
    agents.set_person(db, "laptop", "ann", "admin")
    gallery = TestClient(create_gallery_app(db), base_url="https://testserver")
    started = _ask_connect(gallery, "laptop")
    page = gallery.get("/connect", params={"code": started["code"]}).text
    request_id = re.search(r'name="request_id" value="([^"]+)"', page).group(1)
    gallery.post(
        "/connect/approve",
        data={
            "request_id": request_id,
            "code": started["code"],
            "name": "",
            "choice": "same",
        },
        headers={"sec-fetch-site": "same-origin"},
    )
    polled = gallery.post(
        "/api/v1/connect/poll", json={"poll_token": started["poll_token"]}
    ).json()
    new = keys.verify(db, polled["key"])
    assert new is not None and new.agent == "laptop"
    # The first use of the new key retires the old one; the agent keeps its person throughout.
    keys.first_use(db, new)
    assert _live(db, "laptop") == 1
    with db.connect() as conn:
        assert agents.person_of(conn, "laptop") == "ann"


def test_a_weekly_rotation_keeps_the_personal_mark(db, world):
    _t, old_id = keys.mint(db, agent="laptop")
    with db.connect() as conn:
        conn.execute("update agent_key set rotating = true where id = %s", (old_id,))
    agents.set_person(db, "laptop", "ann", "admin")
    token, _new_id = keys.rotate(
        db, keys.AgentKey(id=old_id, agent="laptop", rotating=True)
    )
    # The old key is revoked ('replaced') at the new key's first use: its replacement is live.
    keys.first_use(db, keys.verify(db, token))
    with db.connect() as conn:
        reason = conn.execute(
            "select revoked_reason from agent_key where id = %s", (old_id,)
        ).fetchone()[0]
        assert reason == "replaced"
        assert agents.person_of(conn, "laptop") == "ann"
    assert _live(db, "laptop") == 1


def test_an_expired_key_with_no_other_live_key_keeps_the_mark_on_purpose(db, world):
    """Expiry sets no `revoked_at`, so it is not a retirement: only revoking the last live key
    clears the mark. A rotation or a re-approval after a lapse then still finds it."""
    _t, key_id = keys.mint(db, agent="laptop")
    agents.set_person(db, "laptop", "ann", "admin")
    with db.connect() as conn:
        conn.execute(
            "update agent_key set rotating = true, created_at = %s where id = %s",
            (datetime.now(UTC) - keys.ROTATING_MAX_AGE - timedelta(days=1), key_id),
        )
    assert keys.verify(db, _t) is None  # it has expired: it no longer opens anything
    assert "expired" in keys.refusal_reason(db, _t)
    with db.connect() as conn:
        assert agents.person_of(conn, "laptop") == "ann"
