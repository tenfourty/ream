"""Renaming an agent, and merging one into another (6 Oct 2026).

Jeremy renamed every permanent agent from "seat" to "<area> maintainer". The
rule these tests hold: nothing already published breaks or changes owner, the
old key keeps working as the new name, and the old name still means the new
one wherever somebody types it.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from artifacts import admin, comments, decisions, events, keys, rename, store
from artifacts.app import create_gallery_app
from artifacts.db import jsonb

PAGE = "<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]

# Every column a rename rewrites, as (table, column, extra where).
COLUMNS = [
    ("agent_key", "agent", ""),
    ("artifact", "agent", ""),
    ("version", "created_by", ""),
    ("comment", "author", " and author_kind = 'agent'"),
    ("comment", "resolved_by", ""),
    ("decision", "answered_by", ""),
    ("decision", "recorded_by", ""),
    ("decision", "acknowledged_by", ""),
    ("decision_outcome", "agent", ""),
    ("receipt", "agent", ""),
    ("event", "agent", ""),
    ("event", "actor", ""),
    ("connect_request", "agent", ""),
    ("seat_grant", "agent", ""),
    ("seat_read", "agent", ""),
    ("idempotency", "agent", ""),
]


def _under(db, name: str) -> dict[str, int]:
    out = {}
    with db.connect() as conn:
        for table, col, extra in COLUMNS:
            out[f"{table}.{col}"] = conn.execute(
                f"select count(*) from {table} where {col} = %s{extra}",  # noqa: S608
                (name,),
            ).fetchone()[0]
    return out


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db), base_url="https://testserver")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _populate(db, agent: str, *, session: str = "s1", scope: str = "x:one"):
    """One of everything that names `agent`. Returns (token, page id, decision id)."""
    token, _ = keys.mint(db, agent)
    page = store.publish(
        db, agent, f"{agent} board", PAGE, session=f"{agent}/{session}"
    )
    asked = decisions.ask(
        db, agent=agent, artifact_id=page.id, question="q?", options=OPTIONS
    )
    decisions.record_elsewhere(
        db, asked.id, letter="a", note="said so", source="terminal", recorded_by=agent
    )
    decisions.acknowledge(db, agent, asked.id)
    decisions.record_outcome(db, asked.id, agent=agent, body="done")
    root = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="hm"
    )
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author=agent,
        body="fixed",
        thread_root=root.id,
        agent=agent,
    )
    comments.resolve(db, root.id, by=agent)
    # No path writes an agent into answered_by today (only his page does, as
    # "jeremy"), but a rename that met one would move it.
    legacy = decisions.ask(
        db, agent=agent, artifact_id=page.id, question="old?", options=OPTIONS
    )
    with db.connect() as conn:
        conn.execute(
            "update decision set answer = 'b', answered_at = %s, answered_by = %s"
            " where id = %s",
            (datetime.now(UTC), agent, legacy.id),
        )
        conn.execute(
            "insert into seat_grant (agent, scope) values (%s, %s)", (agent, scope)
        )
        conn.execute(
            "insert into seat_read (agent, session, last_read_at) values (%s, %s, %s)",
            (agent, session, datetime.now(UTC) - timedelta(hours=1)),
        )
        conn.execute(
            "insert into idempotency (agent, route, key, body_sha256, status,"
            " response, expires_at) values (%s, 'r', 'k', 'h', 200, %s, %s)",
            (agent, jsonb({"from": agent}), datetime.now(UTC) + timedelta(hours=1)),
        )
        conn.execute(
            "insert into connect_request (id, code, poll_sha256, from_address,"
            " expires_at, approved_at, agent) values (%s, %s, %s, '1.2.3.4',"
            " %s, %s, %s)",
            (
                f"cr-{agent}",
                f"C-{agent}",
                f"p-{agent}",
                datetime.now(UTC) + timedelta(hours=1),
                datetime.now(UTC),
                agent,
            ),
        )
    return token, page.id, asked.id


def test_a_plain_rename_moves_every_column_and_the_old_key_is_the_new_name(db, api):
    token, page_id, decision_id = _populate(db, "cove-seat")
    before = _under(db, "cove-seat")
    assert all(n > 0 for n in before.values()), before

    counts = rename.rename_agent(db, "cove-seat", "cove-maintainer", dry_run=False)

    assert set(_under(db, "cove-seat").values()) == {0}
    assert _under(db, "cove-maintainer") == before
    for column, n in before.items():
        assert counts[column] == n, column
    assert counts["agent_alias"] == 1

    mine = api.get("/api/v1/artifacts", headers=_auth(token))
    assert mine.status_code == 200
    assert [row["id"] for row in mine.json()] == [page_id]
    assert mine.json()[0]["agent"] == "cove-maintainer"
    asked = api.get(f"/api/v1/artifacts/{page_id}/decisions", headers=_auth(token))
    relayed = {d["id"]: d for d in asked.json()}[decision_id]
    assert relayed["answer"] == "a"
    assert relayed["recorded_by"] == "cove-maintainer"
    assert events.list_for_agent(db, "cove-maintainer") != []


def test_the_dry_run_reports_the_same_counts_and_changes_nothing(db):
    _populate(db, "warpgate-arc")
    before = _under(db, "warpgate-arc")

    dry = rename.rename_agent(db, "warpgate-arc", "warpgate-maintainer", dry_run=True)
    assert _under(db, "warpgate-arc") == before
    assert rename.resolve(db, "warpgate-arc") == "warpgate-arc"

    real = rename.rename_agent(db, "warpgate-arc", "warpgate-maintainer", dry_run=False)
    assert dry == real
    assert sum(real.values()) > 0


def test_running_it_again_changes_nothing_and_succeeds(db):
    _populate(db, "homelab-ops")
    rename.rename_agent(db, "homelab-ops", "homelab-maintainer", dry_run=False)
    after = _under(db, "homelab-maintainer")

    again = rename.rename_agent(db, "homelab-ops", "homelab-maintainer", dry_run=False)
    assert set(again.values()) == {0}
    assert _under(db, "homelab-maintainer") == after


def test_merging_into_an_existing_agent_keeps_both_keys_pages_and_grants(db, api):
    ops_token, ops_page, _ = _populate(db, "homelab-ops", scope="x:shared")
    baba_token, baba_page, _ = _populate(db, "baba-operator", scope="x:shared")
    with db.connect() as conn:
        conn.execute(
            "insert into seat_grant (agent, scope) values ('baba-operator', 'x:own')"
        )
        # the merged-in agent's read of session s1 is the NEWER one, and its
        # stored response for (r, k) expires later: both should win
        now = datetime.now(UTC)
        conn.execute(
            "update seat_read set last_read_at = %s where agent = 'baba-operator'",
            (now,),
        )
        conn.execute(
            "update idempotency set expires_at = %s where agent = 'baba-operator'",
            (now + timedelta(hours=2),),
        )
        conn.execute(
            "insert into seat_read (agent, session, last_read_at)"
            " values ('baba-operator', '', %s)",
            (now,),
        )

    rename.rename_agent(db, "homelab-ops", "homelab-maintainer", dry_run=False)
    rename.rename_agent(db, "baba-operator", "homelab-maintainer", dry_run=False)

    assert set(_under(db, "baba-operator").values()) == {0}
    for token in (ops_token, baba_token):
        listed = api.get("/api/v1/artifacts", headers=_auth(token))
        assert listed.status_code == 200
        assert {row["id"] for row in listed.json()} == {ops_page, baba_page}
    with db.connect() as conn:
        assert (
            conn.execute(
                "select count(*) from agent_key where agent = 'homelab-maintainer'"
            ).fetchone()[0]
            == 2
        )
        scopes = conn.execute(
            "select scope from seat_grant where agent = 'homelab-maintainer'"
            " order by scope"
        ).fetchall()
        assert [s[0] for s in scopes] == ["x:own", "x:shared"]
        reads = conn.execute(
            "select session, last_read_at from seat_read"
            " where agent = 'homelab-maintainer' order by session"
        ).fetchall()
        recent = datetime.now(UTC) - timedelta(minutes=1)
        assert [(s, at > recent) for s, at in reads] == [("", True), ("s1", True)]
        kept = conn.execute(
            "select response from idempotency where agent = 'homelab-maintainer'"
        ).fetchall()
        assert kept == [({"from": "baba-operator"},)]
        aliases = conn.execute(
            "select old, new from agent_alias order by old"
        ).fetchall()
        assert aliases == [
            ("baba-operator", "homelab-maintainer"),
            ("homelab-ops", "homelab-maintainer"),
        ]


def test_the_old_name_still_works_where_a_name_is_typed(db, api, monkeypatch):
    giver, _ = keys.mint(db, "giver")
    _populate(db, "homelab-ops")
    rename.rename_agent(db, "homelab-ops", "homelab-maintainer", dry_run=False)

    # an agent giving a page, in either spelling of the old name
    for typed in ("homelab-ops", "homelab_ops"):
        page = store.publish(db, "giver", f"to {typed}", PAGE)
        given = api.post(
            f"/api/v1/artifacts/{page.id}/transfer",
            json={"to": typed},
            headers=_auth(giver),
        )
        assert given.status_code == 200, given.text
        assert given.json()["to"] == "homelab-maintainer"

    # his give form
    page = store.publish(db, "giver", "by hand", PAGE)
    moved = api.post(
        f"/a/{page.id}/move",
        data={"to": "homelab-ops"},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert moved.status_code == 200, moved.text
    assert store.owner(db, page.id) == "homelab-maintainer"
    assert "to homelab-maintainer" in moved.text

    # the wall's filter, from a link saved before the rename
    store.publish(db, "giver", "Not theirs", PAGE)
    wall = api.get("/?agent=homelab-ops").text
    assert "homelab-ops board" in wall
    assert "Not theirs" not in wall


def test_the_alias_chain_is_one_hop_and_a_cycle_is_refused(db):
    _populate(db, "artifacts-seat")
    rename.rename_agent(db, "artifacts-seat", "artifacts-arc", dry_run=False)
    rename.rename_agent(db, "artifacts-arc", "artifacts-maintainer", dry_run=False)

    assert rename.resolve(db, "artifacts-seat") == "artifacts-maintainer"
    assert rename.resolve(db, "artifacts-arc") == "artifacts-maintainer"

    with pytest.raises(rename.Refused, match="cycle"):
        rename.rename_agent(db, "artifacts-maintainer", "artifacts-arc", dry_run=False)
    with pytest.raises(rename.Refused, match="renamed to artifacts-maintainer"):
        rename.rename_agent(db, "artifacts-maintainer", "artifacts-seat", dry_run=False)
    assert _under(db, "artifacts-maintainer")["artifact.agent"] == 1


def test_refusals(db):
    _populate(db, "cove-seat")
    with pytest.raises(rename.Refused, match="already called"):
        rename.rename_agent(db, "cove-seat", "cove-seat", dry_run=False)
    with pytest.raises(rename.Refused, match="invalid agent name"):
        rename.rename_agent(db, "cove-seat", "Cove_Maintainer", dry_run=False)
    with pytest.raises(rename.Refused, match="reserved"):
        rename.rename_agent(db, "cove-seat", "jeremy", dry_run=False)
    with pytest.raises(rename.Refused, match="no agent named"):
        rename.rename_agent(db, "cove-sea", "cove-maintainer", dry_run=False)
    assert rename.resolve(db, "cove-sea") == "cove-sea"


def test_published_work_still_reads_under_the_new_name(db, api):
    token, page_id, decision_id = _populate(db, "cove-seat")
    rename.rename_agent(db, "cove-seat", "cove-maintainer", dry_run=False)

    assert store.get_owned(db, "cove-maintainer", page_id) is not None
    assert [r.id for r in store.list_for_agent(db, "cove-maintainer")] == [page_id]
    assert store.list_for_agent(db, "cove-seat") == []
    row = decisions.get_owned(db, "cove-maintainer", decision_id)
    assert row.answer == "a" and row.acknowledged_by == "cove-maintainer"
    assert [o.agent for o in decisions.outcomes_for(db, decision_id)] == [
        "cove-maintainer"
    ]
    thread = api.get(
        f"/api/v1/artifacts/{page_id}/comments?include_resolved=true",
        headers=_auth(token),
    )
    assert thread.status_code == 200
    authors = {c["author"] for c in thread.json()}
    assert authors == {"jeremy", "cove-maintainer"}
    page = api.get(f"/api/v1/artifacts/{page_id}", headers=_auth(token))
    assert page.status_code == 200
    # his wall shows the page under its new owner
    assert "cove-maintainer" in api.get("/").text


def test_the_cli_prints_each_column_and_says_dry_run(db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    _populate(db, "warpgate-arc")

    admin.main(["rename-agent", "warpgate-arc", "warpgate-maintainer", "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "artifact.agent\t1" in out
    assert out.strip().splitlines()[-1].startswith("total\t")
    assert rename.resolve(db, "warpgate-arc") == "warpgate-arc"

    admin.main(["rename-agent", "warpgate-arc", "warpgate-maintainer"])
    out = capsys.readouterr().out
    assert "DRY RUN" not in out
    assert rename.resolve(db, "warpgate-arc") == "warpgate-maintainer"

    with pytest.raises(SystemExit) as refused:
        admin.main(["rename-agent", "nobody", "warpgate-maintainer"])
    assert "refused: no agent named nobody" in str(refused.value)


def test_connecting_a_machine_under_an_old_name_means_the_new_one(db):
    import re

    gallery = TestClient(
        create_gallery_app(db), base_url="https://artifacts.tenfourty.site"
    )
    _token, key_id = keys.mint(db, "cove-seat")
    rename.rename_agent(db, "cove-seat", "cove-maintainer", dry_run=False)

    def approve(code):
        page = gallery.get("/connect", params={"code": code}).text
        request_id = re.search(r'name="request_id" value="([^"]+)"', page).group(1)
        return gallery.post(
            "/connect/approve",
            data={"request_id": request_id, "code": code, "name": "cove-seat"},
            headers={"sec-fetch-site": "same-origin"},
        )

    started = gallery.post(
        "/api/v1/connect", json={"name": "x"}, headers={"x-real-ip": "203.0.113.5"}
    ).json()
    refused = approve(started["code"])
    assert "cove-maintainer is already the name" in refused.text
    keys.revoke(db, key_id)
    assert "Approved. cove-maintainer" in approve(started["code"]).text
    assert set(_under(db, "cove-seat").values()) == {0}
