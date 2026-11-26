"""Person #1 and the request's person (people design, slice 1;
docs/agent/specs/2026-11-24-people-design.md): the owner is seeded into `person` at start-up,
`people.current` answers the owner from settings and reads nothing from the request, and a
route that is the owner's alone is refused for anyone else. Nothing here changes a served word.
"""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ream import owner, people, settings


def _rows(db):
    with db.connect() as conn:
        return conn.execute(
            "select id, issuer, subject, email, name from person order by id"
        ).fetchall()


def _request(**headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            "query_string": b"",
        }
    )


def test_the_first_start_seeds_the_owner_as_person_one(db):
    assert _rows(db) == []
    owner.check(db, "jeremy")
    assert [tuple(r) for r in _rows(db)] == [("jeremy", None, None, None, None)]


def test_a_second_start_changes_nothing(db):
    owner.check(db, "jeremy")
    with db.connect() as conn:
        conn.execute("update person set name = 'J' where id = 'jeremy'")
    owner.check(db, "jeremy")
    assert [tuple(r) for r in _rows(db)] == [("jeremy", None, None, None, "J")]


def test_the_pin_the_first_sign_in_wrote_is_copied_once(db):
    with db.connect() as conn:
        conn.execute(
            "insert into service_meta (key, value) values ('oidc_owner_sub', %s)",
            ("https://idp.example/realm abc 123",),
        )
    owner.check(db, "jeremy")
    assert [tuple(r)[:3] for r in _rows(db)] == [
        ("jeremy", "https://idp.example/realm", "abc 123")
    ]
    owner.check(db, "jeremy")
    assert len(_rows(db)) == 1


def test_a_refused_start_seeds_no_one(db):
    owner.check(db, "jeremy")
    with pytest.raises(owner.OwnerMismatch):
        owner.check(db, "ann")
    assert [r[0] for r in _rows(db)] == ["jeremy"]


def test_current_is_the_owner_from_settings():
    person = people.current(_request())
    assert (person.id, person.name, person.is_owner) == ("jeremy", "Jeremy", True)
    with settings.override(owner_id="ann", owner_name="Ann"):
        person = people.current(_request())
        assert (person.id, person.name) == ("ann", "Ann")


@pytest.mark.parametrize(
    "header",
    ["Remote-User", "X-Forwarded-User", "X-Auth-Request-Email", "Remote-Email"],
)
def test_current_reads_no_identity_header(header):
    assert people.current(_request(**{header: "mallory"})).id == "jeremy"


def test_require_owner_refuses_anyone_else_as_not_found():
    owner_person = people.current(_request())
    assert people.require_owner(owner_person) is owner_person
    other = people.Person(id="ann", name="Ann", is_owner=False)
    with pytest.raises(HTTPException) as refused:
        people.require_owner(other)
    assert refused.value.status_code == 404


def test_nobody_but_the_owner_may_sign_in_yet():
    assert people.OPEN_SIGN_IN is False


# --- the settings give person #1 its sign-in identity (slice 4) -------------------------------

ISS = "https://idp.example/realm"


def _pair(db, who="jeremy"):
    with db.connect() as conn:
        row = conn.execute(
            "select issuer, subject from person where id = %s", (who,)
        ).fetchone()
    return tuple(row)


def test_the_settings_fill_the_owners_issuer_and_subject(db):
    with settings.override(oidc_issuer=ISS, oidc_owner_subject="sub-1"):
        owner.check(db, "jeremy")
    assert _pair(db) == (ISS, "sub-1")


def test_the_settings_win_over_a_pin_and_a_warning_names_no_value(db, caplog):
    with db.connect() as conn:
        conn.execute(
            "insert into service_meta (key, value) values ('oidc_owner_sub', %s)",
            (ISS + " pinned-sub",),
        )
    owner.check(db, "jeremy")
    assert _pair(db) == (ISS, "pinned-sub")
    with settings.override(oidc_issuer=ISS, oidc_owner_subject="sub-2"):
        with caplog.at_level("WARNING", logger="ream.owner"):
            owner.check(db, "jeremy")
        assert _pair(db) == (ISS, "sub-2")
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "settings were used" in text
        assert "sub-2" not in text and "pinned-sub" not in text and ISS not in text
        caplog.clear()
        owner.check(db, "jeremy")  # now equal: no second warning
        assert caplog.records == []


def test_without_both_settings_the_row_is_left_alone(db):
    with settings.override(oidc_issuer=ISS):
        owner.check(db, "jeremy")
    assert _pair(db) == (None, None)


def test_start_is_refused_when_another_person_holds_the_pair(db):
    owner.check(db, "jeremy")
    with db.connect() as conn:
        conn.execute(
            "insert into person (id, issuer, subject) values ('ann', %s, 'sub-1')",
            (ISS,),
        )
    with (
        settings.override(oidc_issuer=ISS, oidc_owner_subject="sub-1"),
        pytest.raises(owner.IdentityHeld),
    ):
        owner.check(db, "jeremy")
    assert _pair(db) == (None, None)
