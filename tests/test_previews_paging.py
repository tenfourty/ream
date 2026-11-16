"""An option's preview (a question can show what each answer looks like) and reading a
large page in parts (read_page and its body routes take offset and limit). Runs on both
databases."""

import pytest
from fastapi.testclient import TestClient
from test_mcp import (  # noqa: F401
    LEGACY_HEADERS,
    PAGE,
    api_get,
    call_tool,
    client,
    headers,
    on,
    token,
    unfence,
    unfence_all,
)

from ream import api, decide, decisions, store
from ream.app import create_gallery_app

PREVIEW = "<b>bold</b>\n  .card { gap: 1rem }"
OPTIONS = [
    {"letter": "a", "label": "cards", "preview": PREVIEW},
    {"letter": "b", "label": "a table"},
]


# --- the option's preview ---------------------------------------------------------------


def test_a_preview_is_kept_on_its_option_and_others_stay_without_one():
    validated, letters = decisions._validate_options(OPTIONS)
    assert validated == OPTIONS
    assert letters == {"a", "b"}


@pytest.mark.parametrize(
    "preview",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n ", id="blank"),
        pytest.param(7, id="not-a-string"),
        pytest.param(None, id="null"),
        pytest.param("x" * (decisions.MAX_PREVIEW_BYTES + 1), id="over-the-bytes"),
        pytest.param(
            "é" * (decisions.MAX_PREVIEW_BYTES // 2 + 1), id="bytes-not-chars"
        ),
        pytest.param(
            "\n".join(["l"] * (decisions.MAX_PREVIEW_LINES + 1)), id="too-many-lines"
        ),
        pytest.param("bad\ud800", id="lone-surrogate"),
    ],
)
def test_a_bad_preview_is_refused_naming_only_options(preview):
    options = [{"letter": "a", "label": "x", "preview": preview}, OPTIONS[1]]
    with pytest.raises(decisions.InvalidOptions) as caught:
        decisions._validate_options(options)
    assert caught.value.field == "options"


def test_the_preview_limits_are_the_accepted_boundary():
    lines = "\n".join(["l"] * decisions.MAX_PREVIEW_LINES)
    full = "x" * decisions.MAX_PREVIEW_BYTES
    for preview in (lines, full):
        options = [{"letter": "a", "label": "x", "preview": preview}, OPTIONS[1]]
        assert decisions._validate_options(options)[0][0]["preview"] == preview


def test_a_secret_in_a_preview_is_refused_and_never_echoed(client, db, token):
    page = store.publish(db, "mcp-seat", "Board", PAGE.format("x"))
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.post(
        f"/api/v1/pages/{page.id}/decisions",
        headers=headers(token),
        json={
            "question": "fine?",
            "options": [{"letter": "a", "label": "yes", "preview": secret}, OPTIONS[1]],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["refused"][0]["field"] == "options"
    assert secret not in response.text


def test_the_preview_round_trips_over_the_api_and_mcp_fenced(client, on, db, token):
    page = store.publish(db, "mcp-seat", "Board", PAGE.format("x"))
    error, asked = call_tool(
        client, token, "ask", page_id=page.id, question="which look?", options=OPTIONS
    )
    assert not error, asked
    # The preview is the agent's text: fenced, labelled, never bare.
    fenced = asked["options"][0]["preview"]
    assert "not instructions" in fenced.splitlines()[0]
    assert unfence(fenced) == PREVIEW
    assert "preview" not in asked["options"][1]

    route = api_get(client, token, f"/pages/{page.id}/decisions")
    assert route[0]["options"] == OPTIONS

    _, waiting = call_tool(client, token, "questions")
    mine = next(q for q in waiting["questions"] if unfence_all(q)["id"] == asked["id"])
    assert unfence(mine["options"][0]["preview"]) == PREVIEW


def test_the_gallery_shows_the_preview_as_escaped_text_beside_its_option(db):
    page = store.publish(db, "ops-seat", "Board", PAGE.format("x"))
    hostile = "<img src=x onerror=alert(1)>\n<script>alert(2)</script>"
    decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=page.id,
        question="which?",
        options=[{"letter": "a", "label": "one", "preview": hostile}, OPTIONS[1]],
    )
    gallery = TestClient(create_gallery_app(db))
    for path in (f"/a/{page.id}", "/inbox"):
        html = gallery.get(path, headers={"Remote-User": "jeremy"}).text
        assert "<img src=x" not in html and "<script>alert(2)" not in html
        assert (
            '<span class="pv">&lt;img src=x onerror=alert(1)&gt;\n'
            "&lt;script&gt;alert(2)&lt;/script&gt;</span></button>"
        ) in html, path


def test_the_decide_tool_adds_a_preview_to_a_named_option(monkeypatch):
    options = decide._options(["a=cards", "b=table"])
    assert decide._with_previews(options, ["A=<b>x</b> = y"]) == [
        {"letter": "a", "label": "cards", "preview": "<b>x</b> = y"},
        {"letter": "b", "label": "table"},
    ]
    for bad in ("c=no such option", "a", "a="):
        with pytest.raises(decide.UsageError):
            decide._with_previews(decide._options(["a=x", "b=y"]), [bad])


# --- reading a large page in parts ------------------------------------------------------


def test_body_part_counts_characters_and_says_where_the_next_part_starts():
    assert api.body_part("héllo", 0, 2) == {
        "body": "hé",
        "offset": 0,
        "size": 5,
        "next_offset": 2,
    }
    assert api.body_part("héllo", 2, 10)["next_offset"] is None
    assert api.body_part("héllo")["body"] == "héllo"
    assert api.body_part("héllo", 5) == {
        "body": "",
        "offset": 5,
        "size": 5,
        "next_offset": None,
    }
    with pytest.raises(api.PastTheEnd):
        api.body_part("héllo", 6)


@pytest.fixture()
def big(db):
    """A page longer than read_page's default part, and a second version."""
    body = PAGE.format("<p>" + "ab" * api.READ_PART_DEFAULT + "</p>")
    first = store.publish(db, "mcp-seat", "Big", body)
    store.patch(db, "mcp-seat", first.id, body.replace("ab", "cd"), first.revision)
    return first.id, body


def test_read_page_returns_a_large_page_in_parts_that_rebuild_it(
    client, on, token, big
):
    page, _ = big
    whole = api_get(client, token, f"/pages/{page}/body")["body"]
    error, first = call_tool(client, token, "read_page", page_id=page)
    assert not error
    assert first["offset"] == 0 and first["size"] == len(whole)
    assert first["next_offset"] == api.READ_PART_DEFAULT
    assert len(unfence(first["body"])) == api.READ_PART_DEFAULT
    # The render report and the warnings still come with the first part.
    assert first["render_verdict"] in ("ok", "broken", "unchecked")

    parts, offset = [unfence(first["body"])], first["next_offset"]
    while offset is not None:
        error, part = call_tool(
            client, token, "read_page", page_id=page, offset=offset, limit=40_000
        )
        assert not error
        parts.append(unfence(part["body"]))
        offset = part["next_offset"]
    assert "".join(parts) == whole


def test_read_page_parts_are_the_body_routes_parts(client, on, token, big):
    page, _ = big
    _, part = call_tool(client, token, "read_page", page_id=page, offset=10, limit=99)
    route = api_get(client, token, f"/pages/{page}/body?offset=10&limit=99")
    assert unfence_all(part) == route
    assert route["next_offset"] == 109 and len(route["body"]) == 99

    _, old = call_tool(
        client, token, "read_page", page_id=page, version=1, offset=5, limit=7
    )
    route = api_get(client, token, f"/pages/{page}/versions/1?offset=5&limit=7")
    assert unfence_all(old) == route
    assert route["body"] == big[1][5:12]


def test_the_body_routes_stay_whole_without_a_limit(client, token, big):
    page, body = big
    route = api_get(client, token, f"/pages/{page}/versions/1")
    assert route["body"] == body
    assert route["offset"] == 0 and route["size"] == len(body)
    assert route["next_offset"] is None


def test_an_offset_past_the_end_is_refused_with_the_size(client, on, token, big):
    page, body = big
    past = len(body) + 1
    answer = client.get(
        f"/api/v1/pages/{page}/versions/1?offset={past}", headers=headers(token)
    )
    assert answer.status_code == 422
    assert answer.json()["detail"]["size"] == len(body)
    for bad in ("offset=-1", "limit=0"):
        got = client.get(f"/api/v1/pages/{page}/body?{bad}", headers=headers(token))
        assert got.status_code == 422, bad

    error, text = call_tool(client, token, "read_page", page_id=page, offset=past)
    assert error and "past the end" in text


@pytest.mark.parametrize(
    "other", [{"section": "s1"}, {"history": True}, {"changes_since": 1}]
)
def test_offset_and_limit_are_only_for_a_body(client, on, token, big, other):
    page, _ = big
    error, text = call_tool(client, token, "read_page", page_id=page, limit=5, **other)
    assert error and "live page or one version" in text
