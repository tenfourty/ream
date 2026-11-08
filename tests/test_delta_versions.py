"""Every version kept, stored as zstd deltas (docs/agent/specs/2026-10-26-delta-versions-design.md).

The current version is plain text in `version.body`; an older one is `packed`, a full zstd
copy every 100 versions and a delta against the previous version in between. Nothing an agent
or a reader can see changes, and these tests are mostly that claim, checked from outside.
"""

import hashlib
import random

import pytest
from fastapi.testclient import TestClient

from ream import admin, frame_pass, keys, packing, repack, store
from ream.app import create_gallery_app
from ream.content import create_content_app

HEAD = "<!doctype html><html><head><title>Board</title></head><body>\n"
TAIL = "</body></html>\n"


def _bodies(count: int, *, seed: int = 7, lines: int = 120) -> list[str]:
    """`count` page bodies, each a small random edit of the one before it, and
    never equal to it (an identical body mints no version)."""
    rng = random.Random(seed)
    rows = [
        f"<p>row {i} {rng.random():.8f} {'x' * rng.randint(5, 60)}</p>\n"
        for i in range(lines)
    ]
    out = []
    for n in range(count):
        at = rng.randrange(lines)
        rows[at] = f"<p>edit {n} {rng.random():.8f} é {'y' * rng.randint(1, 40)}</p>\n"
        out.append(HEAD + "".join(rows) + TAIL)
    return out


def _write(db, bodies: list[str], agent: str = "seat"):
    first = store.publish(db, agent, "Board", bodies[0])
    revision = first.revision
    for body in bodies[1:]:
        revision = store.patch(db, agent, first.id, body, revision).revision
    return first.id


def _row(db, artifact_id: str, n: int):
    with db.connect() as conn:
        return conn.execute(
            "select body, packed, codec, base_n, body_bytes, body_sha256, revision"
            " from version where artifact_id = %s and n = %s",
            (artifact_id, n),
        ).fetchone()


def _make_legacy(db, artifact_id: str, bodies: list[str]) -> None:
    """Put the page back as today's code (before this change) left it: every
    body in full, none of the new columns filled."""
    with db.connect() as conn, conn.transaction():
        for n, body in enumerate(bodies, start=1):
            conn.execute(
                "update version set body = %s, packed = null, codec = null,"
                " base_n = null, body_bytes = null, body_sha256 = null"
                " where artifact_id = %s and n = %s",
                (body, artifact_id, n),
            )


@pytest.fixture(autouse=True)
def _cold_cache():
    store.forget_rebuilt()
    yield
    store.forget_rebuilt()


# --- the encoding -------------------------------------------------------------


def test_a_delta_round_trips_and_is_far_smaller_than_the_page():
    a, b = _bodies(2, lines=2000)
    full = packing.pack(1, a, None)
    delta = packing.pack(2, b, a)
    assert full.codec == "zstd1-full" and full.base_n is None
    assert delta.codec == "zstd1-delta" and delta.base_n == 1
    assert len(delta.data) < len(full.data) / 10
    assert packing.unpack(delta.codec, delta.data, a, packing.sha256(b)) == b


def test_keyframes_are_version_one_and_every_hundredth_after():
    assert [n for n in range(1, 302) if packing.is_keyframe(n)] == [1, 101, 201, 301]


def test_unpacking_against_the_wrong_hash_raises():
    a, b = _bodies(2)
    delta = packing.pack(2, b, a)
    with pytest.raises(packing.PackError):
        packing.unpack(delta.codec, delta.data, a, "0" * 64)


def test_an_unknown_codec_raises():
    with pytest.raises(packing.PackError):
        packing.unpack("zstd9-full", b"", None, "0" * 64)


# --- round trip ----------------------------------------------------------------


def test_250_versions_read_back_exactly_and_full_copies_land_at_1_101_201(db):
    bodies = _bodies(250)
    artifact_id = _write(db, bodies)
    for n, body in enumerate(bodies, start=1):
        row = _row(db, artifact_id, n)
        assert row[6] == hashlib.sha256(f"{n}:{body}".encode()).hexdigest()[:16]
        assert row[4] == len(body.encode())
        assert row[5] == hashlib.sha256(body.encode()).hexdigest()
        if n in (1, 101, 201):
            assert (row[2], row[3]) == ("zstd1-full", None)
        else:
            assert (row[2], row[3]) == ("zstd1-delta", n - 1)
        # Only the current version keeps its plain text.
        assert row[0] == (body if n == 250 else "")
        assert row[1]
    for n, body in enumerate(bodies, start=1):
        assert store.get_version_body(db, "seat", artifact_id, n).body == body
    store.forget_rebuilt()
    # And in one walk, in any order, as pagediff asks for two.
    with db.connect() as conn:
        got = store.version_bodies(conn, artifact_id, [250, 3, 150, 101, 100, 1])
    assert got == {n: bodies[n - 1] for n in (250, 3, 150, 101, 100, 1)}


def test_an_unknown_version_is_absent_not_an_error(db):
    artifact_id = _write(db, _bodies(2))
    with db.connect() as conn:
        assert store.version_bodies(
            conn, artifact_id, [1, 2, 9]
        ) == store.version_bodies(conn, artifact_id, [1, 2])
        assert 9 not in store.version_bodies(conn, artifact_id, [9])


def test_the_current_body_is_served_without_decoding(db, monkeypatch):
    artifact_id = _write(db, _bodies(5))

    def no_decoding(*args, **kwargs):
        raise AssertionError("the hot path must not decode")

    monkeypatch.setattr(packing, "unpack", no_decoding)
    body = store.get_body(db, "seat", artifact_id).body
    assert body == _bodies(5)[-1]
    with db.connect() as conn:
        assert store.version_bodies(conn, artifact_id, [5]) == {5: body}


# --- corruption ----------------------------------------------------------------


def test_a_flipped_byte_in_a_delta_raises_for_it_and_every_later_one_in_its_chain(db):
    bodies = _bodies(130)
    artifact_id = _write(db, bodies)
    packed = _row(db, artifact_id, 50)[1]
    broken = bytearray(packed)
    broken[len(broken) // 2] ^= 0xFF
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "update version set packed = %s where artifact_id = %s and n = 50",
            (bytes(broken), artifact_id),
        )
    for n in (50, 51, 100):
        with pytest.raises(packing.PackError):
            store.get_version_body(db, "seat", artifact_id, n)
    # Before it, and from the next full copy on, the chain is whole.
    for n in (1, 49, 101, 130):
        assert store.get_version_body(db, "seat", artifact_id, n).body == bodies[n - 1]


def test_a_stored_hash_that_does_not_match_raises_rather_than_returning_text(db):
    bodies = _bodies(5)
    artifact_id = _write(db, bodies)
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "update version set body_sha256 = %s where artifact_id = %s and n = 2",
            ("0" * 64, artifact_id),
        )
    with pytest.raises(packing.PackError):
        store.get_version_body(db, "seat", artifact_id, 2)


# --- behaviour nothing may change -----------------------------------------------


def test_revision_the_identical_republish_and_the_listing_are_unchanged(db):
    one = HEAD + "<p>café ☃</p>\n" + TAIL
    two = one.replace("caf", "cafe ")
    page = store.publish(db, "seat", "Board", one)
    assert page.revision == hashlib.sha256(f"1:{one}".encode()).hexdigest()[:16]
    again = store.patch(db, "seat", page.id, one, page.revision)
    assert again.unchanged and again.version == 1
    second = store.patch(db, "seat", page.id, two, page.revision)
    assert second.version == 2
    assert second.revision == hashlib.sha256(f"2:{two}".encode()).hexdigest()[:16]
    # The same body again is still a no-op, now that v1 is packed and v2 is current.
    assert store.patch(db, "seat", page.id, two, second.revision).unchanged
    with db.connect() as conn:
        assert conn.execute(
            "select count(*) from version where artifact_id = %s", (page.id,)
        ).fetchone() == (2,)
    listed = {v.n: v for v in store.list_versions(db, "seat", page.id)}
    assert listed[1].bytes == len(one.encode()) != len(one)
    assert listed[2].bytes == len(two.encode())
    assert [v.live for v in store.list_versions(db, "seat", page.id)] == [True, False]


def test_a_stale_revision_still_conflicts_and_leaves_the_page_as_it_was(db):
    bodies = _bodies(3)
    page = store.publish(db, "seat", "Board", bodies[0])
    store.patch(db, "seat", page.id, bodies[1], page.revision)
    with pytest.raises(store.RevisionMismatch):
        store.patch(db, "seat", page.id, bodies[2], page.revision)
    assert _row(db, page.id, 1)[0] == ""  # v1 packed and blanked
    assert _row(db, page.id, 2)[0] == bodies[1]  # v2 is still the plain current
    assert store.get_body(db, "seat", page.id).body == bodies[1]


def test_the_wall_shows_the_current_versions_text(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    first = HEAD + '<p class="standfirst">The first thing it said.</p>\n' + TAIL
    later = first.replace("The first thing it said.", "What it says now.")
    page = store.publish(db, "seat", "Board", first)
    store.patch(db, "seat", page.id, later, page.revision)
    html = TestClient(create_gallery_app(db=db)).get("/").text
    assert "What it says now." in html
    assert "The first thing it said." not in html


def test_the_content_server_serves_an_old_version_and_its_changes_view(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    keys.mint(db, agent="seat")
    bodies = [HEAD + f"<p>generation {i}</p>\n" + TAIL for i in range(1, 6)]
    artifact_id = _write(db, bodies)
    content = TestClient(create_content_app(db))
    content.headers.update({"host": f"{artifact_id}.tenfourty.com"})
    framed = {"sec-fetch-dest": "iframe"}
    for n in (1, 3, 5):
        got = content.get(f"/p/{frame_pass.mint(db, artifact_id, n)}/", headers=framed)
        assert got.status_code == 200
        assert f"generation {n}" in got.text
    live = content.get(f"/p/{frame_pass.mint(db, artifact_id, None)}/", headers=framed)
    assert "generation 5" in live.text


# --- the backfill ----------------------------------------------------------------


def test_the_backfill_converts_a_database_written_by_todays_code(db):
    bodies = _bodies(130)
    artifact_id = _write(db, bodies)
    _make_legacy(db, artifact_id, bodies)
    # Reads work before it runs: with nothing packed, `body` is the answer.
    for n in (1, 64, 130):
        assert store.get_version_body(db, "seat", artifact_id, n).body == bodies[n - 1]
    assert [v.bytes for v in store.list_versions(db, "seat", artifact_id)] == [
        len(b.encode()) for b in reversed(bodies)
    ]

    report = repack.pack_versions(db)
    assert (report.packed, report.blanked) == (130, 129)
    assert report.saved > 0
    for n, body in enumerate(bodies, start=1):
        row = _row(db, artifact_id, n)
        assert row[0] == (body if n == 130 else "")
        assert row[2] == ("zstd1-full" if n in (1, 101) else "zstd1-delta")
    store.forget_rebuilt()
    for n, body in enumerate(bodies, start=1):
        assert store.get_version_body(db, "seat", artifact_id, n).body == body
    # Revisions are what they were.
    assert (
        _row(db, artifact_id, 77)[6]
        == hashlib.sha256(f"77:{bodies[76]}".encode()).hexdigest()[:16]
    )

    again = repack.pack_versions(db)
    assert (again.packed, again.blanked) == (0, 0)


def test_the_backfill_finishes_a_page_it_was_killed_in(db, monkeypatch):
    bodies = _bodies(40)
    artifact_id = _write(db, bodies)
    _make_legacy(db, artifact_id, bodies)
    real = packing.pack
    calls = {"n": 0}

    def dies_after_fifteen(n, body, base):
        calls["n"] += 1
        if calls["n"] > 15:
            raise KeyboardInterrupt("killed")
        return real(n, body, base)

    monkeypatch.setattr(packing, "pack", dies_after_fifteen)
    with pytest.raises(KeyboardInterrupt):
        repack.pack_versions(db)
    monkeypatch.setattr(packing, "pack", real)
    # Whatever was written is complete or absent; nothing is blanked yet, so every
    # version still reads.
    for n in (1, 15, 16, 40):
        assert store.get_version_body(db, "seat", artifact_id, n).body == bodies[n - 1]
    # Committed in batches of repack.BATCH (10): the first ten are whole, the rest absent.
    assert _row(db, artifact_id, 10)[1] is not None
    assert _row(db, artifact_id, 11)[1] is None
    assert _row(db, artifact_id, 10)[0] == bodies[9]

    report = repack.pack_versions(db)
    assert report.packed == 30 and report.blanked == 39
    store.forget_rebuilt()
    for n, body in enumerate(bodies, start=1):
        assert store.get_version_body(db, "seat", artifact_id, n).body == body


def test_the_backfill_never_blanks_a_version_it_could_not_rebuild(db, monkeypatch):
    bodies = _bodies(6)
    artifact_id = _write(db, bodies)
    _make_legacy(db, artifact_id, bodies)
    real = packing.unpack

    def wrong(codec, data, base, expected_sha256):
        return real(codec, data, base, expected_sha256) + "!"

    others = _bodies(3, seed=99)
    other = _write(db, others, agent="other")
    _make_legacy(db, other, others)
    monkeypatch.setattr(packing, "unpack", wrong)
    report = repack.pack_versions(db)
    # Both pages fail the same way, are counted, and lose nothing.
    assert report.failed == 2 and report.blanked == 0
    for page, texts in ((artifact_id, bodies), (other, others)):
        for n, body in enumerate(texts, start=1):
            assert _row(db, page, n)[0] == body


def test_a_patch_after_a_partial_backfill_never_loses_an_unpacked_version(db):
    """The page is half packed (as step 1 of the rollout leaves a live page):
    a new version packs itself and blanks only a predecessor that is packed."""
    bodies = _bodies(12)
    artifact_id = _write(db, bodies[:10])
    _make_legacy(db, artifact_id, bodies[:10])
    revision = _row(db, artifact_id, 10)[6]
    store.patch(db, "seat", artifact_id, bodies[10], revision)
    # v10 was not packed, so it keeps its text; v11 is packed and plain (current).
    assert _row(db, artifact_id, 10)[0] == bodies[9]
    assert _row(db, artifact_id, 10)[1] is None
    assert _row(db, artifact_id, 11)[1] is not None
    repack.pack_versions(db)
    for n in range(1, 12):
        assert store.get_version_body(db, "seat", artifact_id, n).body == bodies[n - 1]
    assert _row(db, artifact_id, 10)[0] == ""
    assert _row(db, artifact_id, 11)[0] == bodies[10]


def test_the_backfill_command_runs_and_reports(db, monkeypatch, capsys):
    bodies = _bodies(5)
    artifact_id = _write(db, bodies)
    _make_legacy(db, artifact_id, bodies)
    monkeypatch.setattr(admin, "_open_db", lambda: db)
    admin.main(["pack-versions"])
    out = capsys.readouterr().out
    assert "5 packed" in out and "4 blanked" in out
    assert _row(db, artifact_id, 1)[0] == ""


def test_a_deleted_page_is_packed_too(db):
    bodies = _bodies(4)
    artifact_id = _write(db, bodies)
    _make_legacy(db, artifact_id, bodies)
    store.set_deleted(db, artifact_id, True, agent="seat")
    assert repack.pack_versions(db).packed == 4


def test_the_backfill_refuses_to_blank_when_the_stored_text_differs_from_the_rebuild(
    db,
):
    bodies = _bodies(4)
    artifact_id = _write(db, bodies)
    _make_legacy(db, artifact_id, bodies)
    # The pack phase will encode what `body` holds, so make the hash disagree with it
    # afterwards: pack, damage the packed row, then ask for verification.
    pages = repack._pack_page(db, artifact_id)
    assert pages == 4
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "update version set body_sha256 = %s where artifact_id = %s and n = 2",
            ("1" * 64, artifact_id),
        )
    with pytest.raises(packing.PackError):
        repack._verify_and_blank(db, artifact_id)
    for n, body in enumerate(bodies, start=1):
        assert _row(db, artifact_id, n)[0] == body


def test_a_frame_declaring_a_huge_size_is_refused_without_decoding_it():
    import zstandard

    bomb = zstandard.ZstdCompressor(level=1).compress(bytes(200 * 1024 * 1024))
    assert len(bomb) < 100_000
    with pytest.raises(packing.PackError):
        packing.unpack(packing.FULL, bomb, None, "0" * 64)


def test_the_rebuilt_store_is_bounded_in_bytes_not_characters():
    cache = store._Rebuilt(10)
    cache.put(("a", 1, "x"), "é" * 4)  # 4 characters, 8 bytes
    cache.put(("a", 2, "y"), "é" * 4)
    assert cache.get(("a", 1, "x")) is None
    assert cache.get(("a", 2, "y")) == "é" * 4
    cache.put(("a", 3, "z"), "é" * 6)  # 12 bytes: over the whole bound
    assert cache.get(("a", 3, "z")) is None


def test_a_cache_hit_decodes_only_the_versions_after_it(db, monkeypatch):
    bodies = _bodies(30)
    artifact_id = _write(db, bodies)
    store.get_version_body(db, "seat", artifact_id, 20)  # warms the cache up to 20
    seen = []
    real = packing.unpack

    def watching(codec, data, base, sha):
        seen.append(sha)
        return real(codec, data, base, sha)

    monkeypatch.setattr(packing, "unpack", watching)
    assert store.get_version_body(db, "seat", artifact_id, 22).body == bodies[21]
    assert len(seen) == 2  # versions 21 and 22 only


def test_the_backfill_blanks_only_rows_still_as_verified(db, monkeypatch):
    bodies = _bodies(5)
    artifact_id = _write(db, bodies)
    _make_legacy(db, artifact_id, bodies)
    repack._pack_page(db, artifact_id)
    real = packing.unpack
    state = {"done": False}

    def changes_a_row_midway(codec, data, base, sha):
        if not state["done"]:
            state["done"] = True
            with db.connect() as conn:
                conn.execute(
                    "update version set body_sha256 = %s where artifact_id = %s and n = 1",
                    ("f" * 64, artifact_id),
                )
        return real(codec, data, base, sha)

    monkeypatch.setattr(packing, "unpack", changes_a_row_midway)
    blanked, _saved = repack._verify_and_blank(db, artifact_id)
    assert blanked == 3  # versions 2-4; 1 changed after it was verified, 5 is current
    assert _row(db, artifact_id, 1)[0] == bodies[0]
    assert _row(db, artifact_id, 2)[0] == ""


def test_verifying_holds_no_write_lock(db, monkeypatch):
    """Publishing to another page must succeed while a page is being verified."""
    bodies = _bodies(4)
    artifact_id = _write(db, bodies)
    _make_legacy(db, artifact_id, bodies)
    repack._pack_page(db, artifact_id)
    real = packing.unpack
    published = []

    def publishes_meanwhile(codec, data, base, sha):
        if not published:
            published.append(None)  # first, or publish's own check would recurse here
            published[0] = store.publish(db, "other", "Other", bodies[0]).id
        return real(codec, data, base, sha)

    monkeypatch.setattr(packing, "unpack", publishes_meanwhile)
    repack._verify_and_blank(db, artifact_id)
    assert published
