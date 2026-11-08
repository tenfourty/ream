"""The tools an arriving agent runs, handed out openly by the service itself
(homelab-ij8.68). Jeremy, 23 Sep 2026: an open address for the helpers, a
generic skill any agent can use, and "ideally ... just a uvx command"."""

import zipfile

import pytest
from fastapi.testclient import TestClient

from ream.app import create_gallery_app

WHEEL = "artifacts-0.1.0-py3-none-any.whl"


@pytest.fixture()
def tools_dir(tmp_path, monkeypatch):
    with zipfile.ZipFile(tmp_path / WHEEL, "w") as z:
        z.writestr("artifacts/__init__.py", "")
    monkeypatch.setenv("REAM_TOOLS_DIR", str(tmp_path))
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


# The bare directory is no longer here: since 24 Sep it is the one-link page
# uv's --find-links reads, and its own test below holds it to one link.
@pytest.mark.parametrize(
    "name", ["artifacts-9.9.9-py3-none-any.whl", "..%2fetc%2fpasswd", "x"]
)
def test_nothing_else_is_served_from_there(gallery, tools_dir, name):
    assert gallery.get(f"/api/v1/tools/{name}").status_code == 404


def test_no_package_means_a_plain_404_not_a_crash(gallery, tmp_path, monkeypatch):
    monkeypatch.setenv("REAM_TOOLS_DIR", str(tmp_path / "missing"))
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
    monkeypatch.setenv("REAM_TOOLS_DIR", str(tmp_path / "missing"))
    text = gallery.get("/api/v1/agents.txt").text
    assert "not available from this address" in text
    assert "uvx --from" not in text


def test_the_skill_is_open_and_in_the_skill_format(gallery):
    response = gallery.get("/api/v1/skill.md")
    assert response.status_code == 200
    assert response.text.startswith("---\nname: ream\ndescription: ")
    assert "https://artifacts.tenfourty.site/api/v1/agents.txt" in response.text
    assert "art_" not in response.text


def test_the_skill_and_the_orientation_teach_the_review_loop(gallery):
    """An agent learns to ask for a review and wait for the verdict from what
    it already reads, not from --help (page review step 2)."""
    for path in ("/api/v1/skill.md", "/api/v1/agents.txt"):
        text = gallery.get(path).text
        assert "ream ask-review <page-id> [--live|--hold]" in text, (
            path
        )  # 8 Nov 2026 (rename, step 2): the documents teach `ream <verb>`; was artifacts-decide
        assert "ream wait --review <review-id>" in text, (
            path
        )  # same re-take: was artifacts-decide
        assert "7 marks" in text, path
    skill = gallery.get("/api/v1/skill.md").text
    assert "--verdict-only" in skill
    assert "ask-review <page-id> [--live|--hold]" in skill
    assert "By default his marks are held until his verdict" in skill


def test_the_skill_says_a_republish_opens_the_next_round(gallery):
    text = " ".join(gallery.get("/api/v1/skill.md").text.split())
    assert "run the same wait --review again: the next round opens by itself" in text


def test_the_skill_teaches_claiming_and_handing_back_a_review(gallery):
    text = " ".join(gallery.get("/api/v1/skill.md").text.split())
    # Step 2 of the Ream rename: the skill names the command `ream`, not `artifacts-decide`.
    assert (
        "Set REAM_SESSION so wait --review can claim the round; if it exits 8 "
        "another session is acting on it, and if you cannot act, run "
        'ream review-release <id> --reason "…".'
    ) in text


def test_one_page_links_the_one_package_for_find_links(gallery, tools_dir):
    """The stable install line: uv reads this page and picks the package
    (the homelab ops seat, 24 Sep 2026). Only the one file, never a listing."""
    page = gallery.get("/api/v1/tools/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert page.text.count("<a ") == 1
    assert f'href="{WHEEL}"' in page.text


def test_no_package_means_no_find_links_page(gallery, tmp_path, monkeypatch):
    monkeypatch.setenv("REAM_TOOLS_DIR", str(tmp_path / "missing"))
    assert gallery.get("/api/v1/tools/").status_code == 404


def test_the_document_carries_a_stable_line_and_a_machine_readable_one(
    gallery, tools_dir
):
    text = gallery.get("/api/v1/agents.txt").text
    base = "https://artifacts.tenfourty.site/api/v1/tools/"
    assert f"--find-links {base} artifacts" in text
    wheel_lines = [line for line in text.splitlines() if line.startswith("wheel: ")]
    assert wheel_lines == [f"wheel: {base}{WHEEL}"]


@pytest.mark.parametrize(
    ("wheel_name", "package"),
    [
        ("ream-0.1.0-py3-none-any.whl", "ream"),
        ("artifacts-0.1.0-py3-none-any.whl", "artifacts"),
    ],
)
def test_the_install_line_names_the_package_of_the_wheel_served(
    gallery, tmp_path, monkeypatch, wheel_name, package
):
    with zipfile.ZipFile(tmp_path / wheel_name, "w") as z:
        z.writestr("x/__init__.py", "")
    monkeypatch.setenv("REAM_TOOLS_DIR", str(tmp_path))
    from ream import onboarding

    base = "https://artifacts.tenfourty.site/api/v1/tools/"
    text = gallery.get("/api/v1/agents.txt").text
    assert f"--find-links {base} {package}\n" in text
    assert onboarding.agent_card()["cli"]["install"].endswith(
        f"/api/v1/tools/ {package}"
    )


def test_both_exit_code_lists_name_eight_claimed():
    from ream import onboarding

    for text in (onboarding.agents_txt("http://x"), onboarding.skill_md("http://x")):
        assert "6 dismissed 7 marks 8 claimed" in text
