from ream import keys


def test_minted_token_verifies_and_carries_its_agent(db):
    token, _ = keys.mint(db, agent="ops-seat")
    assert token.startswith("art_")
    verified = keys.verify(db, token)
    assert verified is not None and verified.agent == "ops-seat"


def test_plaintext_token_is_never_stored(db):
    token, _ = keys.mint(db, agent="ops-seat")
    with db.connect() as conn:
        stored = conn.execute("select token_sha256 from agent_key").fetchall()
    assert all(token not in row[0] for row in stored)


def test_revoked_token_stops_verifying(db):
    token, key_id = keys.mint(db, agent="ops-seat")
    keys.revoke(db, key_id)
    assert keys.verify(db, token) is None


def test_revoke_returns_the_row_count(db):
    _token, key_id = keys.mint(db, agent="ops-seat")
    assert keys.revoke(db, key_id) == 1


def test_revoke_of_an_unknown_id_changes_nothing(db):
    assert keys.revoke(db, "no-such-id") == 0


def test_revoke_of_an_already_revoked_key_changes_nothing_the_second_time(db):
    _token, key_id = keys.mint(db, agent="ops-seat")
    assert keys.revoke(db, key_id) == 1
    assert keys.revoke(db, key_id) == 0


def test_unknown_and_malformed_tokens_return_none(db):
    assert keys.verify(db, "art_not-a-real-token") is None
    assert keys.verify(db, "") is None
