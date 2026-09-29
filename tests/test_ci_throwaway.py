"""CI's Postgres is relaxed only when declared a throwaway (29 Sep 2026)."""

from conftest import _declared_throwaway


def test_a_dsn_alone_is_never_relaxed():
    assert _declared_throwaway({"PG_TEST_DSN": "postgresql://x/y"}) is None


def test_a_declared_throwaway_is_relaxed():
    env = {"PG_TEST_DSN": "postgresql://x/y", "ARTIFACTS_TEST_PG_THROWAWAY": "1"}
    assert _declared_throwaway(env) == "postgresql://x/y"


def test_the_declaration_alone_names_no_server():
    assert _declared_throwaway({"ARTIFACTS_TEST_PG_THROWAWAY": "1"}) is None
