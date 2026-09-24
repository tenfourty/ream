"""The tools an arriving agent runs, handed out openly by the service itself
(homelab-ij8.68). Jeremy, 23 Sep 2026: an open address for the helpers, a
generic skill any agent can use, and "ideally ... just a uvx command"."""

import zipfile

import pytest
from fastapi.testclient import TestClient

from artifacts.app import create_gallery_app

WHEEL = "artifacts-0.1.0-py3-none-any.whl"


@pytest.fixture()
def tools_dir(tmp_path, monkeypatch):
    with zipfile.ZipFile(tmp_path / WHEEL, "w") as z:
        z.writestr("artifacts/__init__.py", "")
    monkeypatch.setenv("ARTIFACTS_TOOLS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
def gallery(db):
    return TestClient(
        create_gallery_app(db), base_url="https://artifacts.tenfourty.site"
    )


def test_the_package_is_served_openly_by_its_exact_name(gallery, tools_dir):
    response = gallery.get(f"/api/v1/tools/{WHEEL}")
    assert response.status_code == 200
    assert response.content == (tools_dir / WHEEL).read_bytes()
    # an agent's uvx must ask again, or it runs yesterday's tools for ever
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize(
    "name", ["artifacts-9.9.9-py3-none-any.whl", "..%2fetc%2fpasswd", "x", ""]
)
def test_nothing_else_is_served_from_there(gallery, tools_dir, name):
    assert gallery.get(f"/api/v1/tools/{name}").status_code == 404


def test_no_package_means_a_plain_404_not_a_crash(gallery, tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_TOOLS_DIR", str(tmp_path / "missing"))
    assert gallery.get(f"/api/v1/tools/{WHEEL}").status_code == 404


def test_the_onboarding_document_gives_the_uvx_line_for_this_address(
    gallery, tools_dir
):
    text = gallery.get("/api/v1/agents.txt").text
    url = f"https://artifacts.tenfourty.site/api/v1/tools/{WHEEL}"
    assert f"uvx --from {url} artifacts-publish" in text
    assert "git.tenfourty.site" not in text, "the forge is not on the internet"
    assert "https://artifacts.tenfourty.site/api/v1/skill.md" in text


def test_without_a_package_the_document_says_so_rather_than_a_dead_line(
    gallery, tmp_path, monkeypatch
):
    monkeypatch.setenv("ARTIFACTS_TOOLS_DIR", str(tmp_path / "missing"))
    text = gallery.get("/api/v1/agents.txt").text
    assert "not available from this address" in text
    assert "uvx --from" not in text


def test_the_skill_is_open_and_in_the_skill_format(gallery):
    response = gallery.get("/api/v1/skill.md")
    assert response.status_code == 200
    assert response.text.startswith("---\nname: artifacts\ndescription: ")
    assert "https://artifacts.tenfourty.site/api/v1/agents.txt" in response.text
    assert "art_" not in response.text
