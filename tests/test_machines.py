"""The machines page: see every connected machine as a card, rename it, revoke it
(homelab-ij8.103, Jeremy's confirmed ask of 23 Sep 2026; the card redesign, 27-28 Nov 2026)."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import keys, store
from ream.app import create_gallery_app

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
PAGE = "<!doctype html><html><head><title>t</title></head><body>x</body></html>"


@pytest.fixture()
def gallery(db):
    return TestClient(create_gallery_app(db))


def _card(page: str, name: str) -> str:
    """The one card whose name is `name`, as markup."""
    (card,) = re.findall(
        r'<article class="mx-card[^"]*"[^>]*><div class="mx-top"><h3 class="mx-name">'
        + re.escape(name)
        + r"</h3>.*?</article>",
        page,
        flags=re.DOTALL,
    )
    return card


def _age(db, key_id, *, created=None, used=None, rotating=None):
    now = datetime.now(UTC)
    with db.connect() as conn:
        if created is not None:
            conn.execute(
                "update agent_key set created_at = %s where id = %s",
                (now - created, key_id),
            )
        if used is not None:
            conn.execute(
                "update agent_key set last_used_at = %s where id = %s",
                (now - used, key_id),
            )
        if rotating is not None:
            conn.execute(
                "update agent_key set rotating = %s where id = %s", (rotating, key_id)
            )


def test_every_gallery_page_has_his_menu_with_machines_and_connect(gallery):
    page = gallery.get("/").text
    assert 'class="menu profile"' in page
    assert 'href="/machines"' in page and 'href="/connect"' in page


def test_the_page_lists_machines_with_what_he_needs_and_no_secret(db, gallery):
    token, _ = keys.mint(db, agent="laptop")
    store.publish(db, "laptop", "Board", PAGE, session="claude/abc")
    gallery.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    page = gallery.get("/machines").text
    assert '<h3 class="mx-name">laptop</h3>' in page
    assert "<li><code>claude/abc</code></li>" in page
    assert re.search(r"<dt>Pages</dt><dd>1</dd>", page)
    assert "not since it connected" not in page, "last used is recorded"
    assert token not in page and "art_" not in page
    assert "sha256" not in page


def test_he_renames_a_machine_and_its_pages_keep_their_identity(db, gallery):
    _t, key_id = keys.mint(db, agent="laptop")
    gallery.post(
        f"/machines/{key_id}/rename",
        data={"label": "Jeremy's MacBook"},
        headers=SAME_ORIGIN,
    )
    page = gallery.get("/machines").text
    assert '<h3 class="mx-name">Jeremy&#39;s MacBook</h3>' in page
    assert "files pages as <code>laptop</code>" in page


def test_revoke_asks_once_more_then_the_key_stops_working(db, gallery):
    token, key_id = keys.mint(db, agent="laptop")
    auth = {"Authorization": f"Bearer {token}"}
    confirm = gallery.get(f"/machines?revoke={key_id}").text
    assert "Its key stops working at once" in confirm
    assert gallery.get("/api/v1/me", headers=auth).status_code == 200
    done = gallery.post(f"/machines/{key_id}/revoke", headers=SAME_ORIGIN)
    assert "Revoked. That key no longer works." in done.text
    assert gallery.get("/api/v1/me", headers=auth).status_code == 401
    assert "Revoked (1)" in gallery.get("/machines").text


def test_another_site_cannot_revoke_or_rename_with_his_session(db, gallery):
    token, key_id = keys.mint(db, agent="laptop")
    cross = {"sec-fetch-site": "cross-site"}
    assert gallery.post(f"/machines/{key_id}/revoke", headers=cross).status_code == 403
    assert (
        gallery.post(
            f"/machines/{key_id}/rename", data={"label": "x"}, headers=cross
        ).status_code
        == 403
    )
    assert (
        gallery.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )


def test_last_used_is_not_written_on_every_request(db, gallery):
    token, key_id = keys.mint(db, agent="laptop")
    auth = {"Authorization": f"Bearer {token}"}
    gallery.get("/api/v1/me", headers=auth)
    with db.connect() as conn:
        first = conn.execute(
            "select last_used_at from agent_key where id = %s", (key_id,)
        ).fetchone()[0]
    gallery.get("/api/v1/me", headers=auth)
    with db.connect() as conn:
        second = conn.execute(
            "select last_used_at from agent_key where id = %s", (key_id,)
        ).fetchone()[0]
    assert first is not None and first == second


def test_sessions_are_a_short_list_with_each_label_escaped_once(db, gallery):
    """A label is agent-supplied: it comes out escaped, once, inside the card's own markup (an
    `escape()` is Markup, so `"<li>" + escape(x)` would show the tags as text)."""
    for i in range(6):
        keys.mint(db, agent=f"box{i}")
        store.publish(db, f"box{i}", f"B{i}", PAGE, session=f"agent/s{i}")
    keys.mint(db, agent="evil")
    store.publish(db, "evil", "E", PAGE, session="<script>alert(1)</script>")
    page = gallery.get("/machines").text
    assert "&lt;li&gt;" not in page and "&lt;dd&gt;" not in page
    assert page.count('<div class="mx-sess"><h4>Recent sessions</h4><ul>') == 7
    assert "<li><code>agent/s3</code></li>" in page
    assert "<script>alert(1)" not in page
    assert "<li><code>&lt;script&gt;alert(1)&lt;/script&gt;</code></li>" in page


def test_a_machine_shows_three_sessions_and_the_rest_behind_show_all(db, gallery):
    keys.mint(db, agent="busy")
    for i in range(5):
        store.publish(db, "busy", f"P{i}", PAGE, session=f"a/s{i}")
    page = gallery.get("/machines").text
    shown, _, rest = page.partition('<details class="mx-more">')
    assert shown.count("<li><code>a/s") == 3
    assert rest.count("<li><code>a/s") == 2
    assert "Show all 5" in rest
    keys.mint(db, agent="quiet")
    assert "None labelled yet" in gallery.get("/machines").text


def test_machines_lists_only_five_newest_sessions_per_agent(db):
    keys.mint(db, agent="busy")
    for i in range(7):
        store.publish(db, "busy", f"P{i}", PAGE, session=f"a/s{i}")
    (m,) = keys.machines(db)
    assert len(m.sessions) == 5
    assert m.sessions[0] == "a/s6"


def test_each_machine_is_one_card_with_name_owner_badge_and_last_seen(db, gallery):
    _t, key_id = keys.mint(db, agent="laptop")
    _age(db, key_id, used=timedelta(hours=2))
    page = gallery.get("/machines").text
    card = _card(page, "laptop")
    assert page.count('<article class="mx-card') == 1
    assert '<span class="mx-badge ok">Live</span>' in card
    assert 'data-who="shared"' in card and "Shared</span>" in card
    assert "<dt>Last seen</dt><dd>2h ago</dd>" in card
    assert "<dt>Pages</dt><dd>0</dd>" in card
    assert 'class="mx-sub"' in card and "<dl" in card
    assert "Connect another machine" in page and "1 with a working key" in page
    assert f'action="/machines/{key_id}/rename"' in card
    assert f'href="/machines?revoke={key_id}"' in card


_DAY = timedelta(days=1)
_HOUR = timedelta(hours=1)


@pytest.mark.parametrize(
    ("created", "used", "rotating", "css", "text", "check"),
    [
        (_DAY, _HOUR, False, "ok", "Live", None),
        (_DAY, None, False, "warn", "Never used", "Not used since it connected"),
        (30 * _DAY, 20 * _DAY, False, "warn", "Idle", "Not seen for 20 days"),
        (10 * _DAY, _HOUR, True, "bad", "Swap overdue", "Weekly swap is 3 days late"),
        (
            7 * _DAY + _HOUR,
            _HOUR,
            True,
            "bad",
            "Swap overdue",
            "Weekly swap is due now",
        ),
        (
            8 * _DAY + _HOUR,
            _HOUR,
            True,
            "bad",
            "Swap overdue",
            "Weekly swap is 1 day late",
        ),
        (
            15 * _DAY,
            _HOUR,
            True,
            "bad",
            "Expired",
            "Its key expired and is refused; connect it again",
        ),
        (
            15 * _DAY,
            None,
            True,
            "bad",
            "Expired",
            "Its key expired and is refused; connect it again",
        ),
        (15 * _DAY, _HOUR, False, "ok", "Live", None),
        (6 * _DAY, _HOUR, True, "warn", "Key renews soon", None),
        (3 * _DAY, _HOUR, True, "ok", "Live", None),
    ],
)
def test_the_key_state_is_a_badge_and_only_the_worrying_ones_need_attention(
    db, gallery, created, used, rotating, css, text, check
):
    _t, key_id = keys.mint(db, agent="box")
    if used is None:
        _age(db, key_id, created=created, rotating=rotating)
    else:
        _age(db, key_id, created=created, used=used, rotating=rotating)
    card = _card(gallery.get("/machines").text, "box")
    assert f'<span class="mx-badge {css}">{text}</span>' in card
    assert ("data-attn=" in card) is (check is not None)
    if check:
        assert '<dt>Check</dt><dd class="mx-why' in card and check in card
    else:
        assert "<dt>Check</dt>" not in card


def test_a_revoked_machine_is_a_folded_card_with_the_revoked_badge(db, gallery):
    _t, key_id = keys.mint(db, agent="old")
    keys.revoke(db, key_id, actor="jeremy")
    page = gallery.get("/machines").text
    fold = page[page.index('<details class="mx-fold mx-gone">') :]
    assert "<summary>Revoked (1)</summary>" in fold
    assert '<span class="mx-badge off">Revoked</span>' in fold
    assert "<dt>Revoked</dt>" in fold and "(they stay)" in fold
    assert "mx-ctl" not in fold, "no controls on a revoked card"
    assert "0 with a working key" in page


def test_the_filter_counts_each_group_and_marks_cards_for_it(db, gallery):
    _t, plain = keys.mint(db, agent="plain")
    _age(db, plain, used=_HOUR)
    _t, personal = keys.mint(db, agent="hers")
    _age(db, personal, used=_HOUR)
    _t, stale = keys.mint(db, agent="stale")
    _age(db, stale, used=30 * _DAY)
    with db.connect() as conn:
        conn.execute("insert into person (id, name) values ('ann', 'Ann')")
    gallery.post(
        f"/machines/{personal}/belongs",
        data={"belongs_to": "ann"},
        headers=SAME_ORIGIN,
    )
    page = gallery.get("/machines").text
    counts = dict(
        re.findall(r'for="mx-f-(\w+)">[A-Za-z ]+<span class="mx-n">(\d+)</span>', page)
    )
    assert counts == {"all": "3", "shared": "2", "personal": "1", "attention": "1"}
    assert 'id="mx-f-all" checked' in page
    assert 'data-who="personal"' in _card(page, "hers")
    assert "Ann’s</span>" in _card(page, "hers")
    assert 'data-attn="warn"' in _card(page, "stale")
    assert "data-attn" not in _card(page, "plain")
    assert "Nothing needs attention" not in page


def test_a_group_with_no_machine_says_so(db, gallery):
    _t, key_id = keys.mint(db, agent="fine")
    _age(db, key_id, used=_HOUR)
    page = gallery.get("/machines").text
    assert '<p class="mx-empty" data-for="attention">Nothing needs attention.' in page
    assert '<p class="mx-empty" data-for="personal">No machines here.</p>' in page
    assert 'data-for="all"' not in page


def test_every_value_on_a_card_is_escaped(db, gallery):
    hostile = '<img src=x onerror=alert(1)>"&'
    _t, key_id = keys.mint(db, agent="evil-agent")
    gallery.post(
        f"/machines/{key_id}/rename", data={"label": hostile}, headers=SAME_ORIGIN
    )
    store.publish(db, "evil-agent", "E", PAGE, session=hostile)
    with db.connect() as conn:
        conn.execute("insert into person (id, name) values ('mal', %s)", (hostile,))
    gallery.post(
        f"/machines/{key_id}/belongs", data={"belongs_to": "mal"}, headers=SAME_ORIGIN
    )
    for url in ("/machines", f"/machines?revoke={key_id}"):
        page = gallery.get(url).text
        assert "<img src=x" not in page
        assert "&lt;img src=x onerror=alert(1)&gt;" in page


def test_the_revoke_confirmation_replaces_the_controls_on_that_card_only(db, gallery):
    _t, one = keys.mint(db, agent="one")
    keys.mint(db, agent="two")
    page = gallery.get(f"/machines?revoke={one}").text
    asked, other = _card(page, "one"), _card(page, "two")
    assert "Its key stops working at once" in asked
    assert f'action="/machines/{one}/revoke"' in asked
    assert "mx-ctl" not in asked
    assert "mx-ctl" in other and "Its key stops working" not in other


def test_an_expired_weekly_key_is_not_counted_as_working_and_needs_attention(
    db, gallery
):
    """The server refuses a weekly key older than keys.ROTATING_MAX_AGE, so the card says
    Expired, the header does not count it, and the filter lists it under Needs attention."""
    _t, fresh = keys.mint(db, agent="fresh")
    _age(db, fresh, created=_DAY, used=_HOUR, rotating=True)
    _t, dead = keys.mint(db, agent="dead")
    _age(
        db,
        dead,
        created=keys.ROTATING_MAX_AGE + _HOUR,
        used=_HOUR,
        rotating=True,
    )
    _t, edge = keys.mint(db, agent="edge")
    _age(db, edge, created=keys.ROTATING_MAX_AGE - _HOUR, used=_HOUR, rotating=True)
    page = gallery.get("/machines").text
    assert "2 with a working key" in page
    assert '<span class="mx-badge bad">Expired</span>' in _card(page, "dead")
    assert 'data-attn="bad"' in _card(page, "dead")
    assert "Expired" not in _card(page, "edge")
    counts = dict(
        re.findall(r'for="mx-f-(\w+)">[A-Za-z ]+<span class="mx-n">(\d+)</span>', page)
    )
    assert counts["all"] == "3" and counts["attention"] == "2"
    with db.connect() as conn:
        token_row = conn.execute(
            "select count(*) from agent_key where id = %s and revoked_at is null",
            (dead,),
        ).fetchone()
    assert token_row[0] == 1, "expired is not revoked: the card keeps its controls"
    assert "mx-ctl" in _card(page, "dead")
