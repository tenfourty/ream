"""The owner as a setting (core-and-plugins design 3.1, 3.2; slice 1).

`owner_id` is a stored value, so these tests hold three things: the id the service writes is the
configured one and no other; a database written for one id refuses to start under another;
and `artifacts-admin change-owner` moves every stored column in one go. The suite itself runs
as our deployment does (`jeremy`, `Jeremy`, `he`: tests/conftest.py); a test of another owner
uses `settings.override`.
"""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime

import pytest

from ream import (
    admin,
    client,
    comments,
    decide,
    decisions,
    folders,
    keys,
    news,
    onboarding,
    owner,
    rename,
    settings,
    store,
    templates,
)
from ream.db import Database, jsonb
from ream.serve import app_from_env
from ream.text import pronoun

PAGE = "<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture(autouse=True)
def _forget_meta_after(request):
    """The service_meta row a test records is not left for a test that has no `db`.

    The `dsn` is fetched before the test runs: this autouse fixture is torn down after `db`, so
    `db` can no longer be asked for at teardown, and a connection of its own is opened instead."""
    dsn = request.getfixturevalue("dsn") if "db" in request.fixturenames else None
    yield
    if dsn is not None:
        database = Database(dsn)
        try:
            with database.connect() as conn:
                conn.execute("delete from service_meta")
        finally:
            database.close()


# --- the settings object -----------------------------------------------------------------


def test_the_suite_runs_as_our_deployment_does():
    s = settings.current()
    assert (s.owner_id, s.owner_name, s.owner_pronoun) == ("jeremy", "Jeremy", "he")


def test_unset_is_neutral(monkeypatch):
    for name in (
        "REAM_OWNER_ID",
        "REAM_OWNER_NAME",
        "REAM_OWNER_PRONOUN",
    ):
        monkeypatch.delenv(name)
    s = settings.current()
    assert (s.owner_id, s.owner_name, s.owner_pronoun) == (
        "owner",
        "the owner",
        "they",
    )


def test_a_blank_variable_is_unset(monkeypatch):
    monkeypatch.setenv("REAM_OWNER_ID", "   ")
    assert settings.current().owner_id == "owner"


def test_it_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("REAM_OWNER_ID", "ann")
    assert settings.current().owner_id == "ann"
    monkeypatch.setenv("REAM_OWNER_ID", "bob")
    assert settings.current().owner_id == "bob"


def test_override_layers_over_the_environment_and_ends_with_the_block():
    with settings.override(owner_name="Ann"):
        assert settings.current().owner_name == "Ann"
        assert settings.current().owner_id == "jeremy"
        with settings.override(owner_name="Bea"):
            assert settings.current().owner_name == "Bea"
        assert settings.current().owner_name == "Ann"
    assert settings.current().owner_name == "Jeremy"
    with pytest.raises(TypeError), settings.override(nonsense="x"):
        pass


def test_validate_for_serve_lists_every_problem_at_once(monkeypatch):
    monkeypatch.setenv("REAM_OWNER_ID", "Has Spaces")
    monkeypatch.setenv("REAM_OWNER_PRONOUN", "xe")
    with pytest.raises(SystemExit) as exc:
        settings.validate_for_serve()
    message = str(exc.value)
    assert "REAM_OWNER_ID" in message
    assert "REAM_OWNER_PRONOUN" in message
    assert "Has Spaces" not in message


def test_the_repr_prints_no_values():
    assert "jeremy" not in repr(settings.current())


# --- pronouns ----------------------------------------------------------------------------


def test_pronoun_forms():
    he, she, they = pronoun("he"), pronoun("she"), pronoun("they")
    assert (he.subject, he.object, he.possessive, he.reflexive) == (
        "he",
        "him",
        "his",
        "himself",
    )
    assert (she.subject, she.object, she.possessive, she.reflexive) == (
        "she",
        "her",
        "her",
        "herself",
    )
    assert (they.subject, they.object, they.possessive, they.reflexive) == (
        "they",
        "them",
        "their",
        "themselves",
    )
    assert she.possessive_pronoun == "hers"


def test_pronoun_verbs_agree_with_the_subject():
    assert pronoun("he").verb("answer") == "answers"
    assert pronoun("they").verb("answer") == "answer"
    assert pronoun("she").verb("reach") == "reaches"
    assert pronoun("he").verb("reply") == "replies"
    assert pronoun("he").verb("stay") == "stays"
    assert (pronoun("he").be, pronoun("they").be) == ("is", "are")
    assert (pronoun("she").have, pronoun("they").have) == ("has", "have")
    assert pronoun("he").cap(pronoun("he").subject) == "He"


def test_an_unknown_pronoun_is_loud():
    with pytest.raises(ValueError):
        pronoun("xe")


# --- the id the service writes -----------------------------------------------------------


def test_the_reserved_agent_name_is_the_owner_id():
    assert keys.is_reserved_agent_name("jeremy")
    assert not keys.is_reserved_agent_name("ann")
    with settings.override(owner_id="ann"):
        assert keys.is_reserved_agent_name("ann")
        assert not keys.is_reserved_agent_name("jeremy")
        assert keys.reserved_agent_names() == {"ann"}


def test_new_key_refuses_the_configured_owner_id(tmp_path):
    with settings.override(owner_id="ann"):
        with pytest.raises(SystemExit):
            admin.main(["new-key", "--agent", "ann", "--out", str(tmp_path / "k")])
        admin.main(["new-key", "--agent", "jeremy", "--out", str(tmp_path / "k2")])


def test_a_folder_the_owner_makes_belongs_to_the_owner_id(db):
    with settings.override(owner_id="ann"), db.connect() as conn:
        folder_id, _slug = folders.create_his(conn, "Reading")
        row = conn.execute(
            "select owner from folder where id = %s", (folder_id,)
        ).fetchone()
    assert row[0] == "ann"
    assert folders.his() == "jeremy"


def test_an_answer_relayed_for_the_owner_is_stored_under_the_owner_id(db):
    page = store.publish(db, "bosun", "Board", PAGE)
    for who, expected in (("jeremy", "jeremy"), ("ann", "ann")):
        with settings.override(owner_id=who):
            asked = decisions.ask(
                db, agent="bosun", artifact_id=page.id, question="q?", options=OPTIONS
            )
            decisions.record_elsewhere(
                db,
                asked.id,
                letter="a",
                note="n",
                source="terminal",
                recorded_by="bosun",
            )
        with db.connect() as conn:
            stored = conn.execute(
                "select answered_by from decision where id = %s", (asked.id,)
            ).fetchone()[0]
        assert stored == expected


def test_a_gallery_comment_resolve_and_reopen_use_the_owner_id(db):
    page = store.publish(db, "bosun", "Board", PAGE)
    with settings.override(owner_id="ann"):
        root = comments.add(
            db,
            artifact_id=page.id,
            author_kind="agent",
            author="bosun",
            body="hello",
            agent="bosun",
        )
        resolved = comments.resolve(db, root.id, by=settings.owner_id(), agent=None)
        assert resolved is not None
        with db.connect() as conn:
            by = conn.execute(
                "select resolved_by from comment where id = %s", (root.id,)
            ).fetchone()[0]
        assert by == "ann"
        reopened = comments.reopen(db, root.id, agent=None)
        assert reopened is not None
        with db.connect() as conn:
            actor = conn.execute(
                "select actor from event where kind = 'thread_reopened'"
            ).fetchone()
        assert actor is not None and actor[0] == "ann"


# --- the guard ---------------------------------------------------------------------------


def test_a_new_database_takes_whatever_it_is_given(db):
    assert owner.stored(db) is None
    assert owner.check(db, "ann") == "ann"
    assert owner.stored(db) == "ann"


def test_the_same_id_starts_again_and_another_refuses(db):
    owner.check(db, "jeremy")
    assert owner.check(db, "jeremy") == "jeremy"
    with pytest.raises(owner.OwnerMismatch) as exc:
        owner.check(db, "owner")
    assert exc.value.stored == "jeremy"
    assert "change-owner" in str(exc.value)
    assert "REAM_OWNER_ID=jeremy" in str(exc.value)
    assert owner.stored(db) == "jeremy"


def test_the_real_entrypoint_refuses_a_database_written_for_another_owner(
    db, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    owner.check(db, "someone-else")
    with pytest.raises(SystemExit) as exc:
        app_from_env()
    assert "someone-else" in str(exc.value)
    assert "REAM_OWNER_ID" in str(exc.value)


def test_the_real_entrypoint_records_the_owner_on_first_start(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    assert owner.stored(db) is None
    app_from_env()
    assert owner.stored(db) == "jeremy"
    app_from_env()  # a second start finds the same id and carries on


def test_the_real_entrypoint_refuses_a_bad_setting_before_touching_the_database(
    db, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    monkeypatch.setenv("REAM_OWNER_ID", "No Good")
    with pytest.raises(SystemExit) as exc:
        app_from_env()
    assert "REAM_OWNER_ID" in str(exc.value)
    assert owner.stored(db) is None


# --- change-owner ------------------------------------------------------------------------

COLUMNS = [
    ("comment", "author", " and author_kind = 'human'"),
    ("comment", "resolved_by", ""),
    ("decision", "answered_by", ""),
    ("event", "actor", ""),
    ("folder", "owner", ""),
    ("folder_alias", "owner", ""),
    ("comment_image", "staged_by", ""),
    ("partner_question", "answered_by", ""),
]


def _count(db, who: str) -> dict[str, int]:
    out = {}
    with db.connect() as conn:
        for table, col, extra in COLUMNS:
            out[f"{table}.{col}"] = conn.execute(
                f"select count(*) from {table} where {col} = %s{extra}",  # noqa: S608
                (who,),
            ).fetchone()[0]
    return out


def _populate(db):
    """One of each row that carries the owner id (as `jeremy`), plus an agent's own rows."""
    page = store.publish(db, "bosun", "Board", PAGE)
    asked = decisions.ask(
        db, agent="bosun", artifact_id=page.id, question="q?", options=OPTIONS
    )
    decisions.record_elsewhere(
        db, asked.id, letter="a", note="n", source="terminal", recorded_by="bosun"
    )
    root = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="hm"
    )
    comments.resolve(db, root.id, by="jeremy", agent=None)
    comments.reopen(db, root.id, agent=None, by="jeremy")
    comments.resolve(db, root.id, by="jeremy", agent=None)
    with db.connect() as conn:
        folders.create_his(conn, "Reading")
        folder_id = conn.execute("select id from folder").fetchone()[0]
        conn.execute(
            "insert into folder_alias (owner, old_slug, folder_id) values (%s, %s, %s)",
            ("jeremy", "old-reading", folder_id),
        )
        conn.execute(
            "insert into comment_image (id, artifact_id, staged_by, content_type,"
            " width, height, byte_size, sha256, bytes, created_at) values"
            " ('img1', %s, 'jeremy', 'image/png', 1, 1, 1, 'ab', %s, %s)",
            (page.id, b"x", datetime.now(UTC)),
        )
        conn.execute(
            "insert into partner_question (source, version, asking_office, title,"
            " options, state, answered_by, created_at, updated_at)"
            " values ('qd:q9', 1, 'warpgate', 't', %s, 'open', 'jeremy', %s, %s)",
            (jsonb(OPTIONS), datetime.now(UTC), datetime.now(UTC)),
        )
    return page


def test_change_owner_moves_every_stored_column_and_records_the_new_id(db):
    _populate(db)
    owner.check(db, "jeremy")
    before = _count(db, "jeremy")
    assert all(n > 0 for n in before.values()), before
    agent_rows = _count(db, "bosun")

    counts = owner.change_owner(db, "ann", dry_run=False)

    assert set(_count(db, "jeremy").values()) == {0}
    assert _count(db, "ann") == before
    assert _count(db, "bosun") == agent_rows
    for column, n in before.items():
        assert counts[column] == n, column
    assert counts["service_meta"] == 1
    assert owner.stored(db) == "ann"
    # the new id is the one the service now starts with, and the old one now refuses
    assert owner.check(db, "ann") == "ann"
    with pytest.raises(owner.OwnerMismatch):
        owner.check(db, "jeremy")


def test_a_dry_run_counts_and_changes_nothing(db):
    _populate(db)
    owner.check(db, "jeremy")
    before = _count(db, "jeremy")
    counts = owner.change_owner(db, "ann", dry_run=True)
    assert counts["folder.owner"] == 1
    assert _count(db, "jeremy") == before
    assert owner.stored(db) == "jeremy"


def test_change_owner_refuses_what_it_should(db):
    with pytest.raises(owner.Refused, match="no owner is recorded"):
        owner.change_owner(db, "ann", dry_run=False)
    owner.check(db, "jeremy")
    with pytest.raises(owner.Refused, match="already"):
        owner.change_owner(db, "jeremy", dry_run=False)
    with pytest.raises(owner.Refused, match="invalid owner id"):
        owner.change_owner(db, "Ann Smith", dry_run=False)
    keys.mint(db, "bosun")
    with pytest.raises(owner.Refused, match="agent"):
        owner.change_owner(db, "bosun", dry_run=False)
    assert owner.stored(db) == "jeremy"


def test_an_agent_renamed_to_the_owner_id_is_still_refused_after_the_change(db):
    owner.check(db, "jeremy")
    owner.change_owner(db, "ann", dry_run=False)
    with settings.override(owner_id="ann"), pytest.raises(rename.Refused):
        rename.rename_agent(db, "bosun", "ann", dry_run=True)


def test_the_cli_prints_each_column_and_says_what_to_set(db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    _populate(db)
    owner.check(db, "jeremy")

    admin.main(["change-owner", "ann", "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "folder.owner\t1" in out
    assert owner.stored(db) == "jeremy"

    admin.main(["change-owner", "ann"])
    out = capsys.readouterr().out
    assert "DRY RUN" not in out
    assert "REAM_OWNER_ID=ann" in out
    assert owner.stored(db) == "ann"

    with pytest.raises(SystemExit) as refused:
        admin.main(["change-owner", "ann"])
    assert "refused:" in str(refused.value)


# --- served text is unchanged ------------------------------------------------------------

# Digests of what an agent is served for our deployment, taken from the code as it was before
# this slice (6 Nov 2026 tree). Re-taken on purpose on 8 Nov 2026 for the rename: the only change is REAM_/X-Ream- for ARTIFACTS_/X-Artifacts- in the served text and --help. Slice 1 changes ids, not words: the day text moves to
# variables (slice 3) these are re-taken on purpose, with the owner settings below.
# Re-taken again on purpose on 8 Nov 2026 for rename step 2 (the name Ream, `ream <verb>` taught first,
# `/api/v1/pages` for `/api/v1/artifacts`). agents.txt, skill.md, agent.json and both --help digests
# changed ONLY in that wording: Ream (formerly artifacts) where the product is named, `ream publish` /
# `ream ask` for the old command names in examples, /pages for /artifacts in routes, and one short
# paragraph saying the old commands and route keep working. The news tail did not move.
# skill.md re-taken once more on 8 Nov 2026 (rename step 3): the shared Ream skill (name: ream, a
# description that fires on any report/plan/chart/diagram/comparison, a "When to use this" section,
# `ream connect`); agents.txt and agent.json did not move.
# agents.txt and skill.md re-taken on 9 Nov 2026 for MCP slice 1: each gains only a short static
# paragraph saying /mcp takes the same key (agent.json is unchanged while REAM_MCP_PUBLIC_URL is unset).
# agents.txt and skill.md re-taken on 9 Nov 2026 for explain, shorten and fix (the kinds paragraph, the body route).
# agents.txt re-taken again, and the artifacts-decide help, on 9 Nov 2026 for the review fixes (em dash, publisher-only kinds, help names the new kinds).
# agents.txt re-taken on 9 Nov 2026 for the default body words and "a reply is always owed" (settling without one is refused).
# agents.txt, skill.md and the artifacts-publish help re-taken on 9 Nov 2026 for the per-page comments setting (step 1: asked for, not yet required).
# agents.txt, skill.md and the artifacts-decide help re-taken on 9 Nov 2026 for delete-comment / restore-comment.
# agents.txt and skill.md re-taken on 9 Nov 2026 for moving an agent to a new machine (one short paragraph, one skill line).
GOLDEN = {
    "agents.txt": "f7a56bb02acc95234e991312fe7853b4c4d9755813797de359409f0683c9814a",
    "skill.md": "6d138b172d9832f365bab3bf825617087e5fc88f1f85f157b809325c8a5ace18",
    "agent.json": "3ac2c3051230d9a6a47a415978b8fd186db5a3ea5bf487c9754449913cd191df",
}
BASE = "https://artifacts.tenfourty.site"


def _served(monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("REAM_TOOLS_DIR", "/nonexistent-dir")
    monkeypatch.setenv("REAM_API_HOST", "agents.example.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", BASE)
    return {
        "agents.txt": onboarding.agents_txt(BASE),
        "skill.md": onboarding.skill_md(BASE),
        "agent.json": json.dumps(onboarding.agent_card(), sort_keys=True),
    }


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_served_text_is_byte_identical_with_our_settings(monkeypatch, name):
    text = _served(monkeypatch)[name]
    assert hashlib.sha256(text.encode()).hexdigest() == GOLDEN[name]


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_served_text_follows_the_owner_settings(monkeypatch, name):
    if name == "agent.json":
        pytest.skip("agent.json names no owner")
    with settings.override(owner_id="ann", owner_name="Ann", owner_pronoun="she"):
        text = _served(monkeypatch)[name]
    assert "Jeremy" not in text and "Ann" in text
    assert hashlib.sha256(text.encode()).hexdigest() != GOLDEN[name]


# --- the rest of the served text, pinned (design section 8) -------------------------------
#
# Today these words say "Jeremy" and "he" in the text itself; slice 3 moves them to variables
# and re-takes the digests on purpose. Until then a change to any of them is a change to what
# an agent is told, so it shows up here. They hold the display words; the stored id is held by
# the tests above.

FOOTER = "<footer>Published from BABA through the homelab artifacts service.</footer>"
NEWS_TAIL = 26  # every entry so far; newer ones are added in front, so these never move
NEWS_TAIL_DIGEST = "08b7ad27fd383101cc42c10f0994c8368d6fa5b33dc1617635d6068b5902f467"
HELP_DIGEST = {
    "artifacts-publish": "15f17397e44e4a050aaa22cabc01e6d462ba19a335c7beb39492430b519f638b",
    "artifacts-decide": "2a31c966178e2ea6ce7fa9f4ecb40c7c4e4b9506890802b05534e75dd3572a10",
}


def _help(parser: argparse.ArgumentParser) -> str:
    """Every `--help` the parser can print: its own, then each subcommand's, in order."""
    out = [parser.format_help()]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                out.append(_help(sub))
    return "\n".join(out)


def test_every_template_ends_with_the_same_footer():
    names = templates.names()
    assert len(names) == 8
    for name in names:
        assert re.findall(r"<footer>.*?</footer>", templates.body(name)) == [FOOTER], (
            name
        )


def test_the_news_tail_is_unchanged():
    tail = news.entries()[-NEWS_TAIL:]
    text = "\n".join(f"{day}|{line}" for day, line in tail)
    assert hashlib.sha256(text.encode()).hexdigest() == NEWS_TAIL_DIGEST


@pytest.mark.parametrize("name", sorted(HELP_DIGEST))
def test_cli_help_is_unchanged(monkeypatch, name):
    monkeypatch.setenv("COLUMNS", "80")
    parser = (client if name == "artifacts-publish" else decide).build_parser()
    text = _help(parser)
    assert hashlib.sha256(text.encode()).hexdigest() == HELP_DIGEST[name]
