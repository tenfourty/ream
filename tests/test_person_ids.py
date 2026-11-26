"""A new person's id (people design, decision D; slice 4): matched by (issuer, subject) only, a
readable id from the login, one name space with agents, never reused.
docs/agent/specs/2026-11-24-people-design.md"""

import pytest

from ream import keys, owner, people, store

ISS = "https://idp.example/realm"
PAGE = "<!doctype html><html><head><title>t</title></head><body>x</body></html>"


def _make(db, sub, preferred=None, email=None, verified=False, name=None):
    return people.find_or_make(
        db,
        ISS,
        sub,
        preferred=preferred,
        email=email,
        email_verified=verified,
        name=name,
    )


def _ids(db):
    with db.connect() as conn:
        return [
            r[0] for r in conn.execute("select id from person order by id").fetchall()
        ]


def test_the_id_is_the_preferred_username_normalised(db):
    owner.check(db, "jeremy")
    p = _make(db, "s1", preferred="Ann O'Neil_Smith", name="Ann")
    assert (p.id, p.name, p.is_owner) == ("ann-o-neil-smith", "Ann", False)


def test_the_id_falls_back_to_a_verified_email_local_part(db):
    owner.check(db, "jeremy")
    assert _make(db, "s1", email="Bob.Jones@x.example", verified=True).id == "bob-jones"


def test_an_unverified_email_names_no_one_and_is_not_stored(db):
    owner.check(db, "jeremy")
    p = _make(db, "s1", email="bob@x.example", verified=False)
    assert p.id == "person"
    with db.connect() as conn:
        assert (
            conn.execute("select email from person where id = 'person'").fetchone()[0]
            is None
        )


def test_the_same_pair_is_the_same_person_and_the_id_never_changes(db):
    owner.check(db, "jeremy")
    first = _make(db, "s1", preferred="ann")
    again = _make(db, "s1", preferred="someone-else", name="Ann B")
    assert again.id == first.id == "ann"
    assert again.name == "Ann B"
    assert _ids(db) == ["ann", "jeremy"]


def test_an_email_never_matches_a_person(db):
    owner.check(db, "jeremy")
    a = _make(db, "s1", preferred="ann", email="a@x.example", verified=True)
    b = _make(db, "s2", preferred="ann", email="a@x.example", verified=True)
    assert (a.id, b.id) == ("ann", "ann-2")


def test_the_same_subject_at_another_issuer_is_another_person(db):
    owner.check(db, "jeremy")
    a = _make(db, "s1", preferred="ann")
    b = people.find_or_make(
        db,
        "https://other",
        "s1",
        preferred="ann",
        email=None,
        email_verified=False,
        name=None,
    )
    assert (a.id, b.id) == ("ann", "ann-2")


def test_a_taken_id_takes_a_suffix_up_to_99_and_then_refuses(db):
    owner.check(db, "jeremy")
    got = [_make(db, f"s{i}", preferred="ann").id for i in range(1, 4)]
    assert got == ["ann", "ann-2", "ann-3"]
    with db.connect() as conn:
        for n in range(4, 100):
            conn.execute("insert into person (id) values (%s)", (f"ann-{n}",))
    with pytest.raises(people.NameTaken):
        _make(db, "s999", preferred="ann")


@pytest.mark.parametrize(
    "sql",
    [
        "insert into agent_key (id, agent, token_sha256) values ('k', 'bosun', 'h')",
        "insert into mcp_agent (agent, person, client_id, host) values ('bosun', 'jeremy', 'c', 'h')",
        "insert into agent_alias (old, new) values ('bosun', 'bosun-maintainer')",
        "insert into agent_alias (old, new) values ('old-bosun', 'bosun')",
    ],
)
def test_a_name_an_agent_holds_is_never_a_persons_id(db, sql):
    owner.check(db, "jeremy")
    with db.connect() as conn:
        conn.execute(sql)
    assert _make(db, "s1", preferred="bosun").id == "bosun-2"


def test_a_name_a_page_publisher_holds_is_never_a_persons_id(db):
    owner.check(db, "jeremy")
    store.publish(db, "bosun", "One", PAGE)
    assert _make(db, "s1", preferred="bosun").id == "bosun-2"


def test_the_owners_id_is_held(db):
    owner.check(db, "jeremy")
    assert _make(db, "s1", preferred="jeremy").id == "jeremy-2"


def test_a_persons_id_is_never_an_agent_name(db):
    owner.check(db, "jeremy")
    _make(db, "s1", preferred="ann")
    with db.connect() as conn, conn.transaction():
        with pytest.raises(keys.NameHeld, match="person's id"):
            keys.refuse_app_agent_name(conn, "ann")
        keys.refuse_app_agent_name(conn, "someone-else")  # no error


def test_a_disabled_persons_id_is_not_reused(db):
    owner.check(db, "jeremy")
    _make(db, "s1", preferred="ann")
    with db.connect() as conn:
        conn.execute(
            "update person set disabled_at = current_timestamp where id = 'ann'"
        )
    assert _make(db, "s2", preferred="ann").id == "ann-2"


def test_a_login_with_no_pair_names_no_one(db):
    owner.check(db, "jeremy")
    with pytest.raises(people.NameTaken):
        people.find_or_make(
            db, "", "s", preferred="x", email=None, email_verified=False, name=None
        )


def test_normalise_id():
    assert people.normalise_id("  --Ünï Cödé!  ") == "n-c-d"
    assert people.normalise_id("###") == "person"
    assert len(people.normalise_id("a" * 200)) == 60


def test_fill_owner_identity_fills_once_and_refuses_a_pair_someone_holds(db):
    owner.check(db, "jeremy")
    with db.connect() as conn:
        assert people.fill_owner_identity(conn, ISS, "s1") is True
        assert people.fill_owner_identity(conn, ISS, "s1") is False
        assert people.fill_owner_identity(conn, ISS, "other") is False
        row = conn.execute(
            "select issuer, subject from person where id = 'jeremy'"
        ).fetchone()
        assert tuple(row) == (ISS, "s1")
        conn.execute(
            "insert into person (id, issuer, subject) values ('ann', %s, 's2')", (ISS,)
        )
        with pytest.raises(people.NameTaken):
            people.fill_owner_identity(conn, ISS, "s2")


def test_a_preferred_name_with_no_ascii_falls_back_to_the_verified_email(db):
    owner.check(db, "jeremy")
    got = _make(db, "s1", preferred="陳大文", email="Ann.Lee@x.example", verified=True)
    assert got.id == "ann-lee"
    unverified = _make(db, "s2", preferred="陳大文", email="bob@x.example")
    assert unverified.id == "person"


def test_find_or_make_never_returns_the_owners_row(db):
    owner.check(db, "jeremy")
    with db.connect() as conn:
        assert people.fill_owner_identity(conn, ISS, "s1") is True
    with pytest.raises(people.NameTaken):
        _make(db, "s1", preferred="jeremy")


def test_fill_owner_identity_refreshes_only_when_asked(db):
    owner.check(db, "jeremy")
    with db.connect() as conn:
        assert people.fill_owner_identity(conn, ISS, "s1") is True
        assert people.fill_owner_identity(conn, ISS, "s2", refresh=True) is True
        assert people.fill_owner_identity(conn, ISS, "s2", refresh=True) is False
        row = conn.execute(
            "select issuer, subject from person where id = 'jeremy'"
        ).fetchone()
        assert tuple(row) == (ISS, "s2")
        conn.execute(
            "insert into person (id, issuer, subject) values ('ann', %s, 's3')", (ISS,)
        )
        with pytest.raises(people.NameTaken):
            people.fill_owner_identity(conn, ISS, "s3", refresh=True)


def test_a_rename_refuses_a_name_a_person_holds(db):
    from ream import rename

    owner.check(db, "jeremy")
    _make(db, "s1", preferred="ann")
    keys.mint(db, agent="alpha")
    with pytest.raises(rename.Refused, match="person's id"):
        rename.rename_agent(db, "alpha", "ann", dry_run=False)
    with pytest.raises(rename.Refused, match="person's id"):
        rename.rename_agent(db, "alpha", "ann", dry_run=True)
    rename.rename_agent(db, "alpha", "beta", dry_run=False)  # a free name still works


def test_an_app_agent_never_takes_a_name_a_person_holds(db):
    from ream import mcp_apps

    owner.check(db, "jeremy")
    with (
        db.connect() as conn
    ):  # a person whose id is the name the app agent would try first
        conn.execute("insert into person (id) values ('ann--claude-ai')")
    made = mcp_apps.agent_for(db, "ann", "https://claude.ai/client", "claude.ai")
    assert made.agent == "ann--claude-ai-2"
