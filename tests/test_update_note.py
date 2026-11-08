"""A whole-page update can carry a history note and pin the revision it read
(7 Oct 2026).

Both existed only on section updates. A generated board (runcove's programme
board, refreshed by a timer) republishes whole, so it could neither say in the
page's history what moved ("2 slices landed") nor refuse to overwrite a hand
edit made since its last publish. The section-updates design promised
`--if-revision` on a whole-page republish as well (warpgate); this is that.
"""

import json

import pytest
from fastapi.testclient import TestClient
from test_client import key_file, live  # noqa: F401 -- fixtures

from ream import client as cli
from ream import keys, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><body><p id='s'>one</p></body></html>"
PAGE2 = "<!doctype html><html><body><p id='s'>two</p></body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def page(client, db):
    token, _ = keys.mint(db, agent="board-seat")
    made = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    return made["id"], made["revision"], token


def history(client, aid, token):
    return client.get(f"/api/v1/artifacts/{aid}/versions", headers=auth(token)).json()


# --- the API -------------------------------------------------------------------


def test_a_whole_page_update_stores_its_note_on_the_new_version(client, page):
    aid, rev, token = page
    r = client.patch(
        f"/api/v1/artifacts/{aid}",
        json={"body": PAGE2, "expected_revision": rev, "note": "2 slices landed"},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    rows = history(client, aid, token)
    assert rows[0]["note"] == "2 slices landed"
    assert rows[1]["note"] is None


def test_without_a_note_the_version_has_none(client, page):
    aid, rev, token = page
    r = client.patch(
        f"/api/v1/artifacts/{aid}",
        json={"body": PAGE2, "expected_revision": rev},
        headers=auth(token),
    )
    assert r.status_code == 200
    assert history(client, aid, token)[0]["note"] is None


def test_a_note_is_cleaned_and_capped_like_a_section_note(client, page):
    aid, rev, token = page
    r = client.patch(
        f"/api/v1/artifacts/{aid}",
        json={
            "body": PAGE2,
            "expected_revision": rev,
            "note": "\x1b[31mred\x1b[0m " + "x" * 1000,
        },
        headers=auth(token),
    )
    assert r.status_code == 200
    note = history(client, aid, token)[0]["note"]
    assert "\x1b" not in note
    assert len(note) <= store.SECTION_NOTE_MAX


def test_a_blank_note_is_no_note(client, page):
    aid, rev, token = page
    client.patch(
        f"/api/v1/artifacts/{aid}",
        json={"body": PAGE2, "expected_revision": rev, "note": "   "},
        headers=auth(token),
    )
    assert history(client, aid, token)[0]["note"] is None


def test_a_secret_in_a_whole_page_note_is_refused_and_nothing_lands(client, db, page):
    aid, rev, token = page
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    r = client.patch(
        f"/api/v1/artifacts/{aid}",
        json={"body": PAGE2, "expected_revision": rev, "note": f"k {secret}"},
        headers=auth(token),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["refused"][0]["field"] == "note"
    assert secret not in r.text
    assert store.get_body(db, "board-seat", aid).body == PAGE


# --- the CLI -------------------------------------------------------------------


def _publish(path, page_file, capsys):
    page_file.write_text(PAGE)
    assert cli.main(["--key-file", str(path), str(page_file), "--title", "B"]) == 0
    return json.loads(capsys.readouterr().out.strip())


def test_cli_update_with_a_note(live, key_file, tmp_path, capsys):
    path, _ = key_file
    f = tmp_path / "board.html"
    made = _publish(path, f, capsys)
    f.write_text(PAGE2)
    rc = cli.main(
        [
            "--key-file",
            str(path),
            str(f),
            "--update",
            made["id"],
            "--note",
            "1 PR ready",
        ]
    )
    assert rc == 0, capsys.readouterr().err
    capsys.readouterr()
    assert cli.main(["--key-file", str(path), "--versions", made["id"]]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.endswith("\t1 PR ready")


@pytest.mark.parametrize("pin", ["revision", "version"])
def test_cli_if_revision_applies_when_the_page_is_as_read(
    live, key_file, tmp_path, capsys, pin
):
    path, _ = key_file
    f = tmp_path / "board.html"
    made = _publish(path, f, capsys)
    # The publish echo carries the hash; a fresh page is version 1.
    given = made["revision"] if pin == "revision" else "1"
    f.write_text(PAGE2)
    rc = cli.main(
        [
            "--key-file",
            str(path),
            str(f),
            "--update",
            made["id"],
            "--if-revision",
            given,
        ]
    )
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert json.loads(out.strip())["revision"] != made["revision"]


def test_cli_if_revision_refuses_when_someone_changed_the_page(
    live, key_file, db, tmp_path, capsys
):
    path, _ = key_file
    f = tmp_path / "board.html"
    made = _publish(path, f, capsys)
    # A hand edit by another session lands after the generator's last publish.
    edit = "<!doctype html><html><body><p id='s'>hand edit</p></body></html>"
    store.patch(db, "ops-seat", made["id"], edit, made["revision"])
    f.write_text(PAGE2)
    rc = cli.main(
        [
            "--key-file",
            str(path),
            str(f),
            "--update",
            made["id"],
            "--if-revision",
            made["revision"],
        ]
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "NOT applied" in err
    assert "--fetch" in err
    assert store.get_body(db, "ops-seat", made["id"]).body == edit
