import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _padded_body(total_bytes: int) -> str:
    """PAGE followed by ASCII filler, sized to `total_bytes` UTF-8 bytes exactly."""
    filler = total_bytes - len(PAGE.encode("utf-8"))
    assert filler >= 0
    return PAGE + ("a" * filler)


def _post_raw_json(client, url, payload, headers):
    """Send `payload` as a body httpx's own `json=` kwarg cannot carry.

    httpx encodes `json=` with `ensure_ascii=False` and immediately UTF-8-encodes
    the result, so a lone surrogate (`"\\udc80"`) raises `UnicodeEncodeError`
    client-side, before any request is built -- it never reaches the server that
    way. Building the body ourselves with `ensure_ascii=True` escapes the
    surrogate as literal ASCII text (`\\udc80`), which travels as plain bytes
    and is only reconstituted into a real Python surrogate by the server's own
    `json.loads` -- exactly what a real misbehaving client would send.
    """
    body = json.dumps(payload, ensure_ascii=True).encode("ascii")
    return client.post(
        url, content=body, headers={**headers, "content-type": "application/json"}
    )


def _patch_raw_json(client, url, payload, headers):
    body = json.dumps(payload, ensure_ascii=True).encode("ascii")
    return client.patch(
        url, content=body, headers={**headers, "content-type": "application/json"}
    )


def _boom(_value):
    """A stand-in for a broken `scan.scan` -- the crash class this used to
    be reachable through (`&#` plus a long decimal digit run) is fixed at
    the source (task 5 fix round 2, item 5), so `store.ScanFailed`'s tests
    now inject a failure directly rather than depend on a specific bug."""
    raise ValueError("scanner exploded")


def test_publishing_returns_a_one_label_url(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    )
    assert response.status_code == 201
    body = response.json()
    assert body["url"] == f"https://{body['id']}.tenfourty.com/"
    assert body["id"].isalnum() and body["id"].islower() and len(body["id"]) == 26


def test_patching_keeps_the_same_url_and_advances_the_version(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    second = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={
            "body": PAGE.replace("hi", "hello"),
            "expected_revision": first["revision"],
        },
        headers=auth(token),
    )
    assert second.status_code == 200
    assert second.json()["url"] == first["url"]
    assert second.json()["version"] == first["version"] + 1


def test_a_stale_revision_is_refused(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={"body": PAGE, "expected_revision": first["revision"]},
        headers=auth(token),
    )
    stale = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={"body": PAGE, "expected_revision": first["revision"]},
        headers=auth(token),
    )
    assert stale.status_code == 409


def test_a_body_with_a_secret_is_refused_and_says_where_not_what(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.post(
        "/api/v1/artifacts",
        json={"title": "Leaky", "body": PAGE + secret},
        headers=auth(token),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == {
        "refused": [{"field": "body", "shape": "aws_access_key_id", "line": 1}]
    }
    assert secret not in response.text


def test_another_agents_key_cannot_read_or_patch(client, db):
    token_a, _ = keys.mint(db, agent="seat-a")
    token_b, _ = keys.mint(db, agent="seat-b")
    mine = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=auth(token_a),
    ).json()
    assert (
        client.get(f"/api/v1/artifacts/{mine['id']}", headers=auth(token_b)).status_code
        == 404
    )
    assert (
        client.patch(
            f"/api/v1/artifacts/{mine['id']}",
            json={"body": PAGE, "expected_revision": mine["revision"]},
            headers=auth(token_b),
        ).status_code
        == 404
    )
    assert mine["id"] not in client.get("/api/v1/artifacts", headers=auth(token_b)).text


def test_patching_another_agents_artifact_over_the_cap_still_returns_413(client, db):
    """The size cap runs before ownership, so it applies to a foreign artifact too --
    but it never distinguishes "foreign" from "yours": either way the caller only
    ever sees 413, never a hint that the id belongs to someone else."""
    token_a, _ = keys.mint(db, agent="seat-a")
    token_b, _ = keys.mint(db, agent="seat-b")
    mine = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=auth(token_a),
    ).json()
    body = _padded_body(store.MAX_BODY_BYTES + 1)
    response = client.patch(
        f"/api/v1/artifacts/{mine['id']}",
        json={"body": body, "expected_revision": mine["revision"]},
        headers=auth(token_b),
    )
    assert response.status_code == 413


def test_patching_another_agents_artifact_with_a_secret_returns_404_not_422(client, db):
    """Pins the ordering: ownership is checked before the scanner ever runs, so a
    secret-laden body under the cap gets the same 404 as any other field would --
    never a 422 that would confirm the artifact exists and belongs to someone else."""
    token_a, _ = keys.mint(db, agent="seat-a")
    token_b, _ = keys.mint(db, agent="seat-b")
    mine = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=auth(token_a),
    ).json()
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.patch(
        f"/api/v1/artifacts/{mine['id']}",
        json={"body": PAGE + secret, "expected_revision": mine["revision"]},
        headers=auth(token_b),
    )
    assert response.status_code == 404


def test_no_key_and_a_forged_identity_header_are_both_refused(client, db):
    assert (
        client.post("/api/v1/artifacts", json={"title": "x", "body": PAGE}).status_code
        == 401
    )
    forged = client.post(
        "/api/v1/artifacts",
        json={"title": "x", "body": PAGE},
        headers={"Remote-User": "jeremy", "Remote-Groups": "admins"},
    )
    assert forged.status_code == 401


def test_a_key_in_a_query_parameter_is_refused(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = client.post(
        f"/api/v1/artifacts?key={token}", json={"title": "x", "body": PAGE}
    )
    assert response.status_code == 401


def test_the_auth_scheme_is_case_insensitive(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers={"Authorization": f"bEaReR {token}"},
    )
    assert response.status_code == 201


def test_a_body_at_the_exact_size_cap_is_accepted(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    body = _padded_body(store.MAX_BODY_BYTES)
    response = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": body}, headers=auth(token)
    )
    assert response.status_code == 201


def test_a_body_one_byte_over_the_cap_is_refused_with_413(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    body = _padded_body(store.MAX_BODY_BYTES + 1)
    response = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": body}, headers=auth(token)
    )
    assert response.status_code == 413


def test_a_secret_in_the_title_is_refused_even_with_a_clean_body(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.post(
        "/api/v1/artifacts",
        json={"title": f"rotate {secret}", "body": PAGE},
        headers=auth(token),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == {
        "refused": [{"field": "title", "shape": "aws_access_key_id", "line": 1}]
    }
    assert secret not in response.text


def test_a_secret_in_the_title_is_distinguishable_from_the_same_secret_in_the_body(
    client, db
):
    """Before `field` existed, a secret on line 1 of the title answered
    byte-for-byte what the same secret on line 1 of the body would -- the
    line number, which exists so an agent can fix the line, pointed at line
    1 of what was actually a clean page."""
    token, _ = keys.mint(db, agent="ops-seat")
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    in_title = client.post(
        "/api/v1/artifacts",
        json={"title": f"rotate {secret}", "body": PAGE},
        headers=auth(token),
    )
    in_body = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE + secret},
        headers=auth(token),
    )
    assert in_title.status_code == in_body.status_code == 422
    assert in_title.json()["detail"] != in_body.json()["detail"]
    assert in_title.json()["detail"]["refused"][0]["field"] == "title"
    assert in_body.json()["detail"]["refused"][0]["field"] == "body"


def test_a_title_at_the_exact_size_cap_is_accepted(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    title = "t" * store.MAX_TITLE_BYTES
    response = client.post(
        "/api/v1/artifacts", json={"title": title, "body": PAGE}, headers=auth(token)
    )
    assert response.status_code == 201


def test_a_title_one_byte_over_the_cap_is_refused_with_413(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    title = "t" * (store.MAX_TITLE_BYTES + 1)
    response = client.post(
        "/api/v1/artifacts", json={"title": title, "body": PAGE}, headers=auth(token)
    )
    assert response.status_code == 413


def test_an_oversized_title_is_reported_as_a_title_refusal_not_a_body_refusal(
    client, db
):
    """Before `field` existed: a 600-byte title with a 75-byte body answered
    `413 {"detail": "body exceeds the size limit"}` and logged
    `reason=body_too_large size=600` -- an agent shrinking its body in
    response would be refused again, identically, forever."""
    token, _ = keys.mint(db, agent="ops-seat")
    title = "é" * 300  # 300 characters, 600 UTF-8 bytes -- over the 512-byte title cap
    response = client.post(
        "/api/v1/artifacts", json={"title": title, "body": PAGE}, headers=auth(token)
    )
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["field"] == "title"
    assert detail["size"] == 600


def test_an_oversized_title_refusal_is_logged_against_the_title_not_the_body(
    client, db, caplog
):
    token, _ = keys.mint(db, agent="ops-seat")
    title = "é" * 300
    with caplog.at_level(logging.WARNING, logger="artifacts.refusals"):
        client.post(
            "/api/v1/artifacts",
            json={"title": title, "body": PAGE},
            headers=auth(token),
        )
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "field=title" in message and "size=600" in message for message in messages
    )


def test_a_lone_surrogate_in_the_title_is_refused_not_a_500(client, db):
    """A lone surrogate survives JSON decoding -- Python's str holds one --
    but neither utf-8 nor Postgres text can, so this must be a 422 refusal,
    never a 500 raised out of the size check's own `.encode("utf-8")`."""
    token, _ = keys.mint(db, agent="ops-seat")
    response = _post_raw_json(
        client,
        "/api/v1/artifacts",
        {"title": "bad\udc80title", "body": PAGE},
        auth(token),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "title"


def test_a_lone_surrogate_in_the_body_is_refused_not_a_500(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = _post_raw_json(
        client,
        "/api/v1/artifacts",
        {"title": "Board", "body": PAGE + "\udc80"},
        auth(token),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "body"


def test_a_lone_surrogate_in_a_patch_body_is_refused_not_a_500(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    response = _patch_raw_json(
        client,
        f"/api/v1/artifacts/{first['id']}",
        {"body": PAGE + "\udc80", "expected_revision": first["revision"]},
        auth(token),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "body"


def test_a_patch_over_the_size_cap_is_refused_with_413_before_scanning(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    body = _padded_body(store.MAX_BODY_BYTES + 1)
    response = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={"body": body, "expected_revision": first["revision"]},
        headers=auth(token),
    )
    assert response.status_code == 413


def test_a_secret_refusal_is_logged_without_the_secret_value(client, db, caplog):
    token, _ = keys.mint(db, agent="ops-seat")
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    with caplog.at_level(logging.WARNING, logger="artifacts.refusals"):
        client.post(
            "/api/v1/artifacts",
            json={"title": "Leaky", "body": PAGE + secret},
            headers=auth(token),
        )
    assert caplog.records
    for record in caplog.records:
        assert secret not in record.getMessage()
    messages = [record.getMessage() for record in caplog.records]
    assert any("ops-seat" in message for message in messages)
    assert any("aws_access_key_id" in message for message in messages)


def test_a_patch_refusal_is_logged_with_the_artifact_id(client, db, caplog):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    with caplog.at_level(logging.WARNING, logger="artifacts.refusals"):
        client.patch(
            f"/api/v1/artifacts/{first['id']}",
            json={"body": PAGE + secret, "expected_revision": first["revision"]},
            headers=auth(token),
        )
    messages = [record.getMessage() for record in caplog.records]
    assert any(first["id"] in message for message in messages)
    for message in messages:
        assert secret not in message


def test_a_size_refusal_is_logged_with_the_size_but_never_the_body(client, db, caplog):
    token, _ = keys.mint(db, agent="ops-seat")
    size = store.MAX_BODY_BYTES + 1
    body = _padded_body(size)
    with caplog.at_level(logging.WARNING, logger="artifacts.refusals"):
        client.post(
            "/api/v1/artifacts",
            json={"title": "Board", "body": body},
            headers=auth(token),
        )
    messages = [record.getMessage() for record in caplog.records]
    assert any("ops-seat" in message and str(size) in message for message in messages)
    for message in messages:
        assert "a" * 1000 not in message


def test_a_patch_size_refusal_is_logged_with_the_artifact_id(client, db, caplog):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    size = store.MAX_BODY_BYTES + 1
    body = _padded_body(size)
    with caplog.at_level(logging.WARNING, logger="artifacts.refusals"):
        client.patch(
            f"/api/v1/artifacts/{first['id']}",
            json={"body": body, "expected_revision": first["revision"]},
            headers=auth(token),
        )
    messages = [record.getMessage() for record in caplog.records]
    assert any(first["id"] in message and str(size) in message for message in messages)
    for message in messages:
        assert "a" * 1000 not in message


def test_a_validation_error_never_echoes_the_offending_value(client, db):
    """`body` must be a string; sending an object trips pydantic's own type
    check before `store.publish` ever sees it. FastAPI's default handler for
    this puts the rejected value in `input` -- exactly the leak the scanner's
    422 is built to avoid, on the framework's error rather than ours."""
    token, _ = keys.mint(db, agent="ops-seat")
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.post(
        "/api/v1/artifacts",
        json={"title": "x", "body": {"k": secret}},
        headers=auth(token),
    )
    assert response.status_code == 422
    assert secret not in response.text
    for error in response.json()["detail"]:
        assert set(error) == {"loc", "type"}


def test_a_missing_field_error_never_echoes_the_rest_of_the_request(client, db):
    """Pydantic's `missing`-field error echoes the *whole* request body by
    default, including fields the model never declared -- worse than the
    type-mismatch case above, since a field named anything at all leaks."""
    token, _ = keys.mint(db, agent="ops-seat")
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.post(
        "/api/v1/artifacts",
        json={"title": "x", "note": secret},  # `body` is missing entirely
        headers=auth(token),
    )
    assert response.status_code == 422
    assert secret not in response.text
    for error in response.json()["detail"]:
        assert set(error) == {"loc", "type"}


def test_a_failed_publish_leaves_no_artifact_row(client, db):
    """Every statement autocommitted independently used to mean the artifact
    row could commit and then the version insert fail, leaving a permanent,
    unpatchable orphan (current_version stays NULL forever). Postgres `text`
    cannot hold a NUL byte, which is the cheapest way to force exactly that
    failure -- wrapping the writes in one transaction is what actually fixes
    it, so this asserts on the row count, not the (still-500) status code."""
    token, _ = keys.mint(db, agent="ops-seat")
    with db.connect() as conn:
        before = conn.execute("select count(*) from artifact").fetchone()[0]
    with pytest.raises(psycopg.DataError):
        client.post(
            "/api/v1/artifacts",
            json={"title": "x", "body": PAGE + "\x00"},
            headers=auth(token),
        )
    with db.connect() as conn:
        after = conn.execute("select count(*) from artifact").fetchone()[0]
    assert after == before


def test_a_lost_patch_race_returns_409_not_500(client, db):
    """Deterministic pin for the UniqueViolation -> RevisionMismatch mapping:
    a version row already occupies the slot `patch()` is about to insert
    into, forcing the exact primary-key collision two truly concurrent
    patches would hit, without depending on real thread timing."""
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    with db.connect() as conn:
        conn.execute(
            "insert into version (artifact_id, n, body, revision, created_by)"
            " values (%s, 2, %s, %s, %s)",
            (first["id"], PAGE, "occupied-slot", "seed"),
        )
    response = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={
            "body": PAGE.replace("hi", "hello"),
            "expected_revision": first["revision"],
        },
        headers=auth(token),
    )
    assert response.status_code == 409


def test_two_real_concurrent_patches_never_return_500(client, db):
    """Two genuine OS threads, synchronised with a barrier so both fire as
    close to simultaneously as possible, each PATCHing the same artifact with
    the same `expected_revision` it read. Exactly one must win (200), the
    other must lose cleanly (409) -- never a raw database error."""
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    barrier = threading.Barrier(2)

    def do_patch(new_body: str):
        barrier.wait()
        return client.patch(
            f"/api/v1/artifacts/{first['id']}",
            json={"body": new_body, "expected_revision": first["revision"]},
            headers=auth(token),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(do_patch, PAGE.replace("hi", "hello"))
        future_b = pool.submit(do_patch, PAGE.replace("hi", "howdy"))
        response_a, response_b = future_a.result(), future_b.result()

    assert sorted([response_a.status_code, response_b.status_code]) == [200, 409]


def test_a_scanner_failure_is_refused_and_leaves_no_row_at_create(
    client, db, monkeypatch
):
    """A scanner exception is not "no findings": it must refuse exactly as a
    real finding would, never fall through to a clean publish."""
    token, _ = keys.mint(db, agent="ops-seat")
    monkeypatch.setattr(store.scan, "scan", _boom)
    with db.connect() as conn:
        before = conn.execute("select count(*) from artifact").fetchone()[0]
    response = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    )
    assert response.status_code == 503
    with db.connect() as conn:
        after = conn.execute("select count(*) from artifact").fetchone()[0]
    assert after == before


def test_a_scanner_failure_is_refused_and_leaves_no_row_at_patch(
    client, db, monkeypatch
):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    with db.connect() as conn:
        before = conn.execute("select count(*) from version").fetchone()[0]
    monkeypatch.setattr(store.scan, "scan", _boom)
    response = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={
            "body": PAGE.replace("hi", "howdy"),
            "expected_revision": first["revision"],
        },
        headers=auth(token),
    )
    assert response.status_code == 503
    with db.connect() as conn:
        after = conn.execute("select count(*) from version").fetchone()[0]
    assert after == before


def test_a_scanner_failure_is_logged_at_error_not_warning(
    client, db, monkeypatch, caplog
):
    """A `scan_failed` line is a defect in the scanner, not a routine
    refusal, and must be distinguishable from one at a glance -- logged at
    ERROR so a later tidy-up cannot quietly drop it back to WARNING and let
    a run of these hide inside a weekly refusal count."""
    token, _ = keys.mint(db, agent="ops-seat")
    monkeypatch.setattr(store.scan, "scan", _boom)
    with caplog.at_level(logging.WARNING, logger="artifacts.refusals"):
        client.post(
            "/api/v1/artifacts",
            json={"title": "Board", "body": PAGE},
            headers=auth(token),
        )
    scan_failed = [r for r in caplog.records if "scan_failed" in r.getMessage()]
    assert scan_failed
    assert all(r.levelno == logging.ERROR for r in scan_failed)
    assert any("ops-seat" in r.getMessage() for r in scan_failed)


def test_a_scanner_failure_answers_503_naming_the_field_and_never_the_content(
    client, db, monkeypatch
):
    """422 tells a well-behaved agent that ITS content is the problem, and it
    would rewrite a perfectly good page and resubmit forever while the real
    defect -- the scanner, ours -- goes unfixed. 503 says plainly the fault
    is ours, nothing published, and not to rewrite and retry."""
    token, _ = keys.mint(db, agent="ops-seat")
    monkeypatch.setattr(store.scan, "scan", _boom)
    marker = "UNIQUE_MARKER_should_never_be_echoed"
    response = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE + marker},
        headers=auth(token),
    )
    assert response.status_code == 503
    detail = response.json()["detail"]
    # publish() guards the title before the body, so a scanner broken for
    # every field is caught on the title first here -- naming it is still
    # the point of the assertion, whichever field it happens to be.
    assert detail["field"] == "title"
    assert "our side" in detail["detail"]
    assert "operator" in detail["detail"]
    assert "not" in detail["detail"]
    assert marker not in response.text


def test_a_scanner_failure_on_patch_also_answers_503(client, db, monkeypatch):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    monkeypatch.setattr(store.scan, "scan", _boom)
    response = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={
            "body": PAGE.replace("hi", "howdy"),
            "expected_revision": first["revision"],
        },
        headers=auth(token),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["field"] == "body"


def test_reading_an_artifact_by_api_reports_its_current_revision_and_url(client, db):
    """`artifacts-publish` (client.py) needs two things neither read route carried
    before: `--update` reads GET /artifacts/{id} for the expected_revision to PATCH
    with (the body itself is never returned to an agent by this route), and `--list`
    reads GET /artifacts for a url per line. Both the single-artifact read and the
    listing must carry both fields."""
    token, _ = keys.mint(db, agent="ops-seat")
    created = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()

    fetched = client.get(
        f"/api/v1/artifacts/{created['id']}", headers=auth(token)
    ).json()
    assert fetched["revision"] == created["revision"]
    assert fetched["url"] == created["url"]

    listed = client.get("/api/v1/artifacts", headers=auth(token)).json()
    assert listed[0]["revision"] == created["revision"]
    assert listed[0]["url"] == created["url"]

    patched = client.patch(
        f"/api/v1/artifacts/{created['id']}",
        json={"body": PAGE.replace("hi", "hello"), "expected_revision": created["revision"]},
        headers=auth(token),
    ).json()

    refetched = client.get(
        f"/api/v1/artifacts/{created['id']}", headers=auth(token)
    ).json()
    assert refetched["revision"] == patched["revision"]
    assert refetched["revision"] != created["revision"]
