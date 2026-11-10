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
    keys.revoke(db, key_id, actor="admin")
    assert keys.verify(db, token) is None


def test_revoke_returns_the_row_count(db):
    _token, key_id = keys.mint(db, agent="ops-seat")
    assert keys.revoke(db, key_id, actor="admin") == 1


def test_revoke_of_an_unknown_id_changes_nothing(db):
    assert keys.revoke(db, "no-such-id", actor="admin") == 0


def test_revoke_of_an_already_revoked_key_changes_nothing_the_second_time(db):
    _token, key_id = keys.mint(db, agent="ops-seat")
    assert keys.revoke(db, key_id, actor="admin") == 1
    assert keys.revoke(db, key_id, actor="admin") == 0


def test_unknown_and_malformed_tokens_return_none(db):
    assert keys.verify(db, "art_not-a-real-token") is None
    assert keys.verify(db, "") is None


def _revocation_events(db):
    with db.connect() as conn:
        return conn.execute(
            "select actor, summary, ref_id from event where kind = 'agent_key_revoked'"
        ).fetchall()


def test_a_revoke_records_who_and_why_and_writes_an_event(db):
    from ream import store

    token, key_id = keys.mint(db, agent="ops-seat")
    store.publish(db, "ops-seat", "board", "<p>x</p>")
    keys.revoke(db, key_id, actor="jeremy", reason="lost laptop")
    with db.connect() as conn:
        row = conn.execute(
            "select revoked_by, revoked_reason from agent_key where id = %s", (key_id,)
        ).fetchone()
    assert tuple(row) == ("jeremy", "lost laptop")
    events = _revocation_events(db)
    assert len(events) == 1
    actor, summary, ref_id = events[0]
    assert (actor, ref_id) == ("jeremy", key_id)
    assert "lost laptop" in summary and token not in summary
    assert keys.revoke(db, key_id, actor="jeremy") == 0
    assert len(_revocation_events(db)) == 1


def test_a_revoked_keys_refusal_names_its_reason_and_nothing_else(db):
    token, key_id = keys.mint(db, agent="ops-seat")
    _other, _ = keys.mint(db, agent="someone-else")
    keys.revoke(db, key_id, actor="admin", reason="folded into cove")
    said = keys.refusal_reason(db, token)
    assert "folded into cove" in said
    assert "someone-else" not in said and token not in said
    assert keys.refusal_reason(db, "art_never-issued") == "unknown or revoked key"


def test_a_move_retirement_records_actor_and_reason_and_writes_an_event(db):
    from ream import store

    _old_token, old_key = keys.mint(db, agent="ops-seat")
    token, new_key = keys.mint(db, agent="ops-seat")
    store.publish(db, "ops-seat", "board", "<p>x</p>")
    with db.connect() as conn:
        conn.execute("update agent_key set rotating = true where id = %s", (new_key,))
        conn.execute(
            "update agent_key set retire_after = %s where id = %s", (new_key, old_key)
        )
    keys.first_use(db, keys.verify(db, token))
    with db.connect() as conn:
        row = conn.execute(
            "select revoked_by, revoked_reason from agent_key where id = %s", (old_key,)
        ).fetchone()
    assert tuple(row) == ("ops-seat", "moved")
    assert [e[2] for e in _revocation_events(db)] == [old_key]


def test_a_routine_rotation_records_the_row_but_writes_no_event(db):
    from ream import store

    _t, old_key = keys.mint(db, agent="ops-seat")
    store.publish(db, "ops-seat", "board", "<p>x</p>")
    with db.connect() as conn:
        conn.execute("update agent_key set rotating = true where id = %s", (old_key,))
    token, new_key = keys.rotate(
        db, keys.AgentKey(id=old_key, agent="ops-seat", rotating=True)
    )
    with db.connect() as conn:
        conn.execute("update agent_key set rotating = true where id = %s", (new_key,))
    keys.first_use(db, keys.verify(db, token))
    with db.connect() as conn:
        row = conn.execute(
            "select revoked_by, revoked_reason from agent_key where id = %s", (old_key,)
        ).fetchone()
    assert tuple(row) == ("ops-seat", "replaced")
    assert _revocation_events(db) == []
