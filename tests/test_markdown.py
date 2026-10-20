"""Publishing a page from markdown (runcove-5t3oj.33, 20 Oct 2026).

An agent hands over markdown and the service renders it into a page in the
house look, through the API (`format: "markdown"`) or the CLI (a `.md` file).
What every office asked for (docs/agent/feedback/2026-10-20-markdown-pages.md):
tables, fenced code, task lists, linkable headings, raw HTML passing through so
a decision slot still works, section updates on a markdown page, and a title
that defaults to the first heading but can be overridden.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import client as cli
from artifacts import keys, markdown, store, templates
from artifacts.app import create_gallery_app

DOC = """# Weekly status

Intro with `code`.

## Done & next

| item | state |
|------|-------|
| cutover | done |

- [x] shipped
- [ ] next

```python
print("hi")
```

<div id="ask" data-artifact-decision="d1"></div>

<details><summary>More</summary>

hidden **bold**

</details>
"""


# ---- the renderer ----------------------------------------------------------


def test_page_has_the_house_look_and_the_heading_in_the_masthead():
    page, title = markdown.render_page(DOC)
    assert title == "Weekly status"
    assert page.startswith("<!doctype html>")
    assert "<title>Weekly status</title>" in page
    # Inlined, so the wall's thumbnail and --preview are styled too.
    assert templates.shared_css()[0] in page
    head, main = page.split("<main>", 1)
    assert "<h1>Weekly status</h1>" in head
    assert "<h1" not in main
    assert "<!--artifacts:updated-->" in head


def test_the_features_offices_asked_for():
    page, _ = markdown.render_page(DOC)
    assert '<div class="table-wrap"><table>' in page and "<th>item</th>" in page
    assert 'class="task-list-item-checkbox" checked="checked"' in page
    assert '<code class="language-python">' in page
    assert '<h2 id="done-next">Done &amp; next</h2>' in page


def test_raw_html_passes_through_so_decision_slots_and_folds_work():
    page, _ = markdown.render_page(DOC)
    assert '<div id="ask" data-artifact-decision="d1"></div>' in page
    assert "<details><summary>More</summary>" in page
    assert "<strong>bold</strong>" in page


def test_a_given_title_wins_over_the_heading_and_is_escaped():
    page, title = markdown.render_page(DOC, "Ops <week 43>")
    assert title == "Ops <week 43>"
    assert "<title>Ops &lt;week 43&gt;</title>" in page


def test_no_heading_and_no_title_names_nothing():
    _, title = markdown.render_page("just a paragraph")
    assert title is None


def test_repeated_headings_get_distinct_ids():
    page, _ = markdown.render_page("# T\n\n## Notes\n\n## Notes\n")
    assert 'id="notes"' in page and 'id="notes-1"' in page


def test_fragment_is_bare_html():
    assert markdown.render_fragment("- one\n- two").strip() == (
        "<ul>\n<li>one</li>\n<li>two</li>\n</ul>"
    )


# ---- the API ---------------------------------------------------------------


@pytest.fixture()
def api(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Europe/Paris")
    token, _ = keys.mint(db, agent="md-seat")
    return TestClient(create_gallery_app(db=db)), {"Authorization": f"Bearer {token}"}


def test_publish_markdown_named_by_its_heading(api, db):
    client, auth = api
    r = client.post(
        "/api/v1/artifacts", json={"body": DOC, "format": "markdown"}, headers=auth
    )
    assert r.status_code == 201, r.text
    made = r.json()
    assert made["title"] == "Weekly status"
    stored = store.get_body(db, "md-seat", made["id"]).body
    assert stored.startswith("<!doctype html>") and "<table>" in stored


def test_markdown_with_neither_title_nor_heading_is_refused(api):
    client, auth = api
    r = client.post(
        "/api/v1/artifacts",
        json={"body": "no heading here", "format": "markdown"},
        headers=auth,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "title"


def test_html_without_a_title_is_still_refused(api):
    client, auth = api
    r = client.post(
        "/api/v1/artifacts",
        json={"body": "<!doctype html><p>x</p>"},
        headers=auth,
    )
    assert r.status_code == 422


def test_html_publish_is_unchanged(api, db):
    client, auth = api
    body = "<!doctype html><title>H</title><p>**not markdown**</p>"
    made = client.post(
        "/api/v1/artifacts", json={"title": "H", "body": body}, headers=auth
    ).json()
    assert store.get_body(db, "md-seat", made["id"]).body == body


def test_update_from_markdown_keeps_the_name(api, db):
    client, auth = api
    made = client.post(
        "/api/v1/artifacts", json={"body": DOC, "format": "markdown"}, headers=auth
    ).json()
    r = client.patch(
        f"/api/v1/artifacts/{made['id']}",
        json={
            "body": "# Renamed heading\n\nnew text",
            "format": "markdown",
            "expected_revision": made["revision"],
        },
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Weekly status"
    assert "new text" in store.get_body(db, "md-seat", made["id"]).body


def test_section_update_with_a_markdown_fragment(api, db):
    client, auth = api
    made = client.post(
        "/api/v1/artifacts",
        json={
            "body": '# Log\n\n<div id="log">\n\nfirst\n\n</div>\n',
            "format": "markdown",
        },
        headers=auth,
    ).json()
    r = client.patch(
        f"/api/v1/artifacts/{made['id']}/sections/log",
        json={"op": "append", "html": "- **second**", "format": "markdown"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = store.get_body(db, "md-seat", made["id"]).body
    assert "<li><strong>second</strong></li>" in body


def test_section_update_targets_a_heading_id(api, db):
    client, auth = api
    made = client.post(
        "/api/v1/artifacts", json={"body": DOC, "format": "markdown"}, headers=auth
    ).json()
    r = client.patch(
        f"/api/v1/artifacts/{made['id']}/sections/done-next",
        json={"op": "replace", "html": "Done, and *next*", "format": "markdown"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert "<em>next</em>" in store.get_body(db, "md-seat", made["id"]).body


def test_render_endpoint_publishes_nothing(api, db):
    client, auth = api
    r = client.post("/api/v1/markdown", json={"body": DOC}, headers=auth)
    assert r.status_code == 200
    assert r.json()["title"] == "Weekly status"
    assert "<table>" in r.json()["html"]
    listed = client.get("/api/v1/artifacts", headers=auth).json()
    assert not (listed.get("artifacts") if isinstance(listed, dict) else listed)


# ---- the CLI ---------------------------------------------------------------


def test_cli_sends_a_md_file_as_markdown_without_a_title(tmp_path, monkeypatch):
    page = tmp_path / "status.md"
    page.write_text("# Status\n\nok\n")
    sent = {}

    def fake_request(method, url, key, payload):
        sent.update(method=method, url=url, payload=payload)
        return {"id": "x"}

    monkeypatch.setattr(cli, "_request", fake_request)
    cli._publish(str(page), None, "k")
    assert sent["payload"] == {
        "title": "",
        "body": "# Status\n\nok\n",
        "format": "markdown",
    }


def test_cli_leaves_html_alone(tmp_path, monkeypatch):
    page = tmp_path / "status.html"
    page.write_text("<!doctype html><p>x</p>")
    sent = {}
    monkeypatch.setattr(
        cli, "_request", lambda m, u, k, p: sent.update(payload=p) or {"id": "x"}
    )
    cli._publish(str(page), "T", "k")
    assert "format" not in sent["payload"]


def test_cli_check_refuses_markdown_with_a_way_forward(tmp_path):
    page = tmp_path / "status.md"
    page.write_text("# Status\n")
    with pytest.raises(cli.UsageError, match="--preview"):
        cli._check_page(str(page))


def test_oversized_markdown_is_refused_before_it_is_rendered(api, monkeypatch):
    client, auth = api
    monkeypatch.setattr(store, "MAX_BODY_BYTES", 100)
    rendered = []
    monkeypatch.setattr(
        markdown, "render_page", lambda *a: rendered.append(a) or ("", None)
    )
    big = "# T\n\n" + "x" * 200
    for path, payload in (
        ("/api/v1/artifacts", {"body": big, "format": "markdown"}),
        ("/api/v1/markdown", {"body": big}),
    ):
        r = client.post(path, json=payload, headers=auth)
        assert r.status_code == 413, (path, r.text)
    assert rendered == []


def test_title_from_heading_is_plain_text():
    _, title = markdown.render_page("# Release **v2** of `foo`\n")
    assert title == "Release v2 of foo"


def test_a_nested_heading_stays_where_it_is():
    page, title = markdown.render_page("- # in a list\n")
    assert title is None and "<li>" in page.split("<main>")[1]


def test_long_headings_get_ids_section_updates_accept():
    page, _ = markdown.render_page("# T\n\n## " + "word " * 60 + "\n")
    ident = page.split('<h2 id="')[1].split('"')[0]
    assert 0 < len(ident) <= 120


def test_fragments_get_no_heading_ids():
    assert "id=" not in markdown.render_fragment("## Notes")


def test_lone_surrogate_is_a_422_not_a_500(api):
    client, auth = api
    r = client.post(
        "/api/v1/markdown",
        content=b'{"body": "# T \\ud800"}',
        headers={**auth, "content-type": "application/json"},
    )
    assert r.status_code == 422
