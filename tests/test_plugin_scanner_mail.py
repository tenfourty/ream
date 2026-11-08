"""The scanner and mail-backend plug-in hooks (core-and-plugins design, 4.2 items 4, 5).
With nothing registered the core behaves as before; these cover what a plug-in adds."""

import logging
import time

import pytest

from ream import ggscan, mail, plugin_api, scanners, store
from ream.scan import Finding

PAGE = "<!doctype html><html><head><title>t</title></head><body><p>{}</p></body></html>"


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setattr(scanners, "_SCANNERS", [])
    monkeypatch.setattr(mail, "_backend", None)
    monkeypatch.setattr(plugin_api, "_failures", {})
    monkeypatch.setattr(ggscan, "scan", lambda body: [])  # the relay: clean
    return plugin_api.PluginAPI("toy")


def test_nothing_registered_runs_nothing(world):
    assert scanners.registered() == []
    assert scanners.run("anything") == []


def test_a_scanner_finding_refuses_a_publish(db, world):
    world.scanner(
        "toy", lambda text: [Finding("toy_shape", 1)] if "BAD" in text else []
    )
    store.publish(db, "seat", "ok", PAGE.format("fine"))
    with pytest.raises(store.SecretFound) as caught:
        store.publish(db, "seat", "bad", PAGE.format("BAD"))
    assert [f.shape for f in caught.value.findings] == ["toy_shape"]


def test_a_clean_page_reaches_the_scanner(db, world):
    seen = []
    world.scanner("toy", lambda text: seen.append(text) or [])
    store.publish(db, "seat", "t", PAGE.format("clean"))
    assert any("clean" in t for t in seen)


def test_a_raising_scanner_with_skip_is_counted_and_the_page_publishes(db, world):
    def boom(text):
        raise RuntimeError("secret detail")

    world.scanner("toy", boom, on_error="skip")
    store.publish(db, "seat", "t", PAGE.format("fine"))
    assert plugin_api.failure_counts() == {"toy:scanner": 1}


def test_a_raising_scanner_with_block_fails_the_publish(db, world):
    def boom(text):
        raise RuntimeError("secret detail")

    world.scanner("toy", boom, on_error="block")
    with pytest.raises(store.ScanFailed):
        store.publish(db, "seat", "t", PAGE.format("fine"))
    assert plugin_api.failure_counts() == {"toy:scanner": 1}


def test_a_malformed_answer_is_an_error_not_a_clean_scan(world):
    world.scanner("toy", lambda text: "clean", on_error="block")
    with pytest.raises(scanners.ScannerBlocked):
        scanners.run("x")


def test_bad_on_error_and_duplicate_names_are_refused(world):
    with pytest.raises(ValueError):
        world.scanner("toy", lambda t: [], on_error="ignore")
    world.scanner("toy", lambda t: [])
    with pytest.raises(ValueError):
        world.scanner("toy", lambda t: [])


def test_the_backend_replaces_mailgun(world, monkeypatch):
    sent = []
    monkeypatch.setattr(
        mail, "_mailgun_send", lambda *a: pytest.fail("Mailgun must not be used")
    )
    world.mail_backend(
        "toy", lambda to, subject, text: sent.append((to, subject, text)), lambda: True
    )
    assert mail.enabled() is True
    mail.send("a@example.com", "s", "t")
    assert sent == [("a@example.com", "s", "t")]


def test_with_no_backend_mailgun_is_asked(world, monkeypatch):
    monkeypatch.setattr(mail, "_mailgun_enabled", lambda: True)
    got = []
    monkeypatch.setattr(mail, "_mailgun_send", lambda *a: got.append(a))
    assert mail.enabled() is True
    mail.send("a@example.com", "s", "t")
    assert got == [("a@example.com", "s", "t")]


def test_a_backend_that_raises_is_a_mail_failure_and_counted(world):
    def boom(*a):
        raise RuntimeError("api key abc")

    world.mail_backend("toy", boom, lambda: True)
    with pytest.raises(mail.MailFailed) as caught:
        mail.send("a@example.com", "s", "t")
    assert "abc" not in str(caught.value)
    assert plugin_api.failure_counts() == {"toy:mail": 1}


def test_a_backend_whose_enabled_raises_is_off(world):
    def boom():
        raise RuntimeError

    world.mail_backend("toy", lambda *a: None, boom)
    assert mail.enabled() is False
    assert plugin_api.failure_counts() == {"toy:mail": 1}


def test_a_second_backend_is_refused(world):
    world.mail_backend("one", lambda *a: None, lambda: True)
    with pytest.raises(ValueError):
        plugin_api.PluginAPI("two").mail_backend("two", lambda *a: None, lambda: True)


@pytest.mark.parametrize(
    "bad",
    [[Finding("", 1)], [Finding("x" * 65, 1)], [Finding(None, 1)], [Finding(3, 1)]],
)
def test_a_malformed_finding_shape_is_a_scanner_error(world, bad):
    world.scanner("toy", lambda text: bad, on_error="block")
    with pytest.raises(scanners.ScannerBlocked):
        scanners.run("x")
    assert plugin_api.failure_counts() == {"toy:scanner": 1}


def test_a_malformed_shape_under_skip_is_counted_and_ignored(world):
    world.scanner("toy", lambda text: [Finding("", 1)], on_error="skip")
    assert scanners.run_detailed("x") == ([], False)
    assert plugin_api.failure_counts() == {"toy:scanner": 1}


def test_a_non_callable_scanner_is_refused(world):
    with pytest.raises(TypeError):
        world.scanner("toy", "not callable")


def test_a_slow_scanner_is_a_scanner_error(world, monkeypatch):
    monkeypatch.setattr(scanners, "SCAN_TIMEOUT_SECONDS", 0.05)
    world.scanner("slow", lambda text: time.sleep(1) or [], on_error="block")
    with pytest.raises(scanners.ScannerBlocked):
        scanners.run("x")
    assert plugin_api.failure_counts() == {"toy:scanner": 1}


def test_a_slow_scanner_under_skip_lets_the_page_publish(db, world, monkeypatch):
    monkeypatch.setattr(scanners, "SCAN_TIMEOUT_SECONDS", 0.05)
    world.scanner("slow", lambda text: time.sleep(1) or [], on_error="skip")
    store.publish(db, "seat", "t", PAGE.format("fine"))
    assert plugin_api.failure_counts() == {"toy:scanner": 1}


def test_a_scanner_that_exits_is_a_scanner_error(world):
    def bye(text):
        raise SystemExit(1)

    world.scanner("toy", bye, on_error="block")
    with pytest.raises(scanners.ScannerBlocked):
        scanners.run("x")
    assert plugin_api.failure_counts() == {"toy:scanner": 1}


def test_a_scanner_that_exits_under_skip_is_no_answer(world):
    def bye(text):
        raise SystemExit(1)

    world.scanner("toy", bye, on_error="skip")
    assert scanners.run_detailed("x") == ([], False)


def test_a_scanners_exception_text_never_reaches_logs_or_the_error(world, caplog):
    def boom(text):
        raise RuntimeError("SECRET-IN-EXCEPTION")

    world.scanner("toy", boom, on_error="block")
    with caplog.at_level(logging.DEBUG), pytest.raises(scanners.ScannerBlocked) as c:
        scanners.run("x")
    assert "SECRET-IN-EXCEPTION" not in caplog.text
    assert "SECRET-IN-EXCEPTION" not in str(c.value)
    assert c.value.__cause__ is None


def test_a_relay_finding_skips_the_plugin_scanners(db, world, monkeypatch):
    asked = []
    world.scanner("toy", lambda text: asked.append(text) or [])
    monkeypatch.setattr(ggscan, "scan", lambda body: [Finding("gg_x", 1)])
    with pytest.raises(store.SecretFound) as caught:
        store.publish(db, "seat", "t", PAGE.format("fine"))
    assert [f.shape for f in caught.value.findings] == ["gg_x"]
    assert asked == []


def test_a_gitguardian_field_is_scanned_by_plugins_and_a_plain_one_is_not(world):
    seen = []
    world.scanner("toy", lambda text: seen.append(text) or [])
    store._check_secrets("plain", "title", agent="seat", artifact_id=None)
    assert seen == []
    store._check_secrets(
        "noted", "note", agent="seat", artifact_id=None, gitguardian=True
    )
    assert seen == ["noted"]


def test_a_backend_that_is_off_refuses_to_send(world):
    sent = []
    world.mail_backend("toy", lambda *a: sent.append(a), lambda: False)
    with pytest.raises(mail.MailFailed):
        mail.send("a@example.com", "s", "t")
    assert sent == []


def test_a_backend_whose_enabled_raises_refuses_to_send(world):
    def boom():
        raise RuntimeError

    sent = []
    world.mail_backend("toy", lambda *a: sent.append(a), boom)
    with pytest.raises(mail.MailFailed):
        mail.send("a@example.com", "s", "t")
    assert sent == []


def test_a_raising_enabled_warns_once_per_process(world, caplog, monkeypatch):
    monkeypatch.setattr(mail, "_enabled_fault_logged", False)

    def boom():
        raise RuntimeError

    world.mail_backend("toy", lambda *a: None, boom)
    with caplog.at_level(logging.DEBUG, logger="ream.mail"):
        for _ in range(3):
            mail.enabled()
    levels = [r.levelno for r in caplog.records]
    assert levels.count(logging.WARNING) == 1
    assert plugin_api.failure_counts() == {"toy:mail": 3}


def test_a_backends_exception_text_never_reaches_logs_or_the_error(world, caplog):
    def boom(*a):
        raise RuntimeError("API-KEY-IN-EXCEPTION")

    world.mail_backend("toy", boom, lambda: True)
    with caplog.at_level(logging.DEBUG), pytest.raises(mail.MailFailed) as c:
        mail.send("a@example.com", "s", "t")
    assert "API-KEY-IN-EXCEPTION" not in caplog.text
    assert "API-KEY-IN-EXCEPTION" not in str(c.value)
    assert c.value.__cause__ is None
