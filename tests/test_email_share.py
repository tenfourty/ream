"""Sharing a page with one person by email (homelab-ij8.16, Jeremy's rulings
of 24 Sep 2026: (1a) a personal link confirmed per device by a code sent to the
same inbox, withdrawable per person; (2a) sent through his Mailgun account)."""

import re

import pytest
from fastapi.testclient import TestClient

from ream import email_share, mail, store
from ream.app import create_app, create_gallery_app

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
PAGE = "<!doctype html><html><head><title>t</title></head><body><p>{}</p></body></html>"


@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    monkeypatch.setenv("REAM_MAILGUN_KEY", "test-key-not-real")
    monkeypatch.setenv("REAM_MAILGUN_API", "https://api.eu.mailgun.net")
    monkeypatch.setenv("REAM_MAILGUN_DOMAIN", "mg.tenfourty.site")
    monkeypatch.setenv("REAM_MAIL_FROM", "Artifacts <artifacts@mg.tenfourty.site>")
    monkeypatch.setattr(
        mail, "send", lambda to, subject, text: sent.append((to, subject, text))
    )
    return sent


@pytest.fixture()
def page(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return store.publish(db, "seat", "Plan", PAGE.format("the private plan"))


@pytest.fixture()
def gallery(db):
    return TestClient(create_gallery_app(db))


def _share(gallery, page, outbox, email="friend@example.com"):
    gallery.post(
        f"/a/{page.id}/share/email", data={"email": email}, headers=SAME_ORIGIN
    )
    to, _subject, text = outbox[-1]
    assert to == email
    return re.search(r"https://\S+/e/\S+/", text).group(0)


def _reader(db, url):
    host, path = re.match(r"https://([^/]+)(/.*)", url).groups()
    client = TestClient(create_app(db), base_url=f"https://{host}")
    return client, path


def test_without_mail_configured_the_page_says_so(db, gallery, page, monkeypatch):
    monkeypatch.delenv("REAM_MAILGUN_KEY", raising=False)
    assert "Email isn&#39;t set up" in gallery.get(f"/a/{page.id}/share").text or (
        "Email isn't set up" in gallery.get(f"/a/{page.id}/share").text
    )


def test_the_share_mail_and_the_door_page_follow_the_configured_owner(
    db, gallery, page, outbox, monkeypatch
):
    from ream import settings

    with settings.override(owner_name="Ann", owner_pronoun="she"):
        url = _share(gallery, page, outbox)
        _to, subject, body = outbox[-1]
        assert subject == "Ann shared \u201cPlan\u201d with you"
        assert body.startswith("Ann has shared a page with you: Plan")
        reader, path = _reader(db, url)
        assert "Ann shared this page with" in reader.get(path).text
    monkeypatch.delenv("REAM_OWNER_NAME")
    monkeypatch.delenv("REAM_OWNER_PRONOUN")
    _share(gallery, page, outbox, email="other@example.com")
    assert outbox[-1][1] == "The owner shared \u201cPlan\u201d with you"


def test_the_whole_journey_link_code_open(db, gallery, page, outbox):
    url = _share(gallery, page, outbox)
    assert "Jeremy shared" in outbox[-1][1] and "Plan" in outbox[-1][1]
    assert "friend@example.com" in gallery.get(f"/a/{page.id}/share").text

    reader, path = _reader(db, url)
    first = reader.get(path)
    assert first.status_code == 200
    assert "the private plan" not in first.text, "not before this device is confirmed"
    assert "f***@example.com" in first.text

    asked = reader.post(path + "code")
    assert "Enter your code" in asked.text
    code = re.search(r"\b(\d{6})\b", outbox[-1][2]).group(1)
    assert outbox[-1][0] == "friend@example.com"

    wrong = reader.post(
        path + "verify", data={"code": "000000" if code != "000000" else "111111"}
    )
    assert "didn" in wrong.text
    ok = reader.post(path + "verify", data={"code": code}, follow_redirects=False)
    assert ok.status_code == 303
    cookie = ok.headers["set-cookie"]
    assert (
        "HttpOnly" in cookie
        and "Secure" in cookie
        and "SameSite=lax" in cookie.replace("Lax", "lax")
    )
    opened = reader.get(path)
    assert "the private plan" in opened.text


def test_a_forwarded_link_does_not_open_on_another_device(db, gallery, page, outbox):
    url = _share(gallery, page, outbox)
    me, path = _reader(db, url)
    me.post(path + "code")
    code = re.search(r"\b(\d{6})\b", outbox[-1][2]).group(1)
    me.post(path + "verify", data={"code": code})
    assert "the private plan" in me.get(path).text
    someone_else, _ = _reader(db, url)
    assert "the private plan" not in someone_else.get(path).text


def test_withdrawing_kills_the_link_and_every_confirmed_device(
    db, gallery, page, outbox
):
    url = _share(gallery, page, outbox)
    reader, path = _reader(db, url)
    reader.post(path + "code")
    code = re.search(r"\b(\d{6})\b", outbox[-1][2]).group(1)
    reader.post(path + "verify", data={"code": code})
    (person,) = email_share.people(db, page.id)
    done = gallery.post(
        f"/a/{page.id}/share/email/{person.id}/withdraw", headers=SAME_ORIGIN
    )
    assert "Withdrawn" in done.text
    gone = reader.get(path)
    assert gone.status_code == 404 and "withdrawn" in gone.text


def test_codes_are_limited(db, gallery, page, outbox):
    url = _share(gallery, page, outbox)
    reader, path = _reader(db, url)
    for _ in range(email_share.CODES_PER_HOUR):
        reader.post(path + "code")
    assert "Too many codes" in reader.post(path + "code").text
    code = re.search(r"\b(\d{6})\b", outbox[-1][2]).group(1)
    for _ in range(email_share.CODE_TRIES):
        reader.post(path + "verify", data={"code": "abcdef"})
    locked = reader.post(path + "verify", data={"code": code}, follow_redirects=False)
    assert locked.status_code == 200, "after five wrong tries even the right code fails"


def test_only_he_can_share_by_email(db, gallery, page, outbox):
    cross = gallery.post(
        f"/a/{page.id}/share/email",
        data={"email": "x@example.com"},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert cross.status_code == 403 and outbox == []


def test_a_bad_address_is_refused_in_words(db, gallery, page, outbox):
    response = gallery.post(
        f"/a/{page.id}/share/email",
        data={"email": "not an address"},
        headers=SAME_ORIGIN,
    )
    assert "look like an email address" in response.text.replace("&#39;", "'")
    assert outbox == []


def test_mailgun_is_called_as_documented_and_the_key_is_never_logged(
    monkeypatch, caplog
):
    captured = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["data"] = request.data.decode()
        return _Resp()

    monkeypatch.setenv("REAM_MAILGUN_KEY", "k-123")
    monkeypatch.setenv("REAM_MAILGUN_API", "https://api.eu.mailgun.net")
    monkeypatch.setenv("REAM_MAILGUN_DOMAIN", "mg.tenfourty.site")
    monkeypatch.setenv("REAM_MAIL_FROM", "Artifacts <artifacts@mg.tenfourty.site>")
    monkeypatch.setattr(mail.urllib.request, "urlopen", fake_urlopen)
    mail.send("a@example.com", "Hi", "Body")
    assert captured["url"] == "https://api.eu.mailgun.net/v3/mg.tenfourty.site/messages"
    assert captured["auth"].startswith("Basic ")
    assert "to=a%40example.com" in captured["data"]
    assert "from=Artifacts+%3Cartifacts%40mg.tenfourty.site%3E" in captured["data"]
    assert "k-123" not in caplog.text
