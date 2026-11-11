"""The `gitguardian` scanner is the page's second opinion (core-and-plugins design, step 9c).

GitGuardian's ggshield second scan used to be core's own (`ggscan`); it is the house plug-in's
scanner named `gitguardian` now, and core asks nobody itself. These pin what core does with that
scanner, using a fake one registered through the plug-in API, as the plug-in does:

* a publish with a finding is refused, and the scanner was asked once;
* in an agent comment's second scan, a `gitguardian` that raises or times out gave no opinion,
  so the comment is left unscanned for the sweeper to ask again (a hold stays under `block`),
  and no other scanner's clean answer stands in for it;
* a start with the old `GGSHIELD_URL` or `GGSHIELD_TOKEN` set and no `gitguardian` scanner
  loaded logs ONE warning that the second scan is off, naming the variables and no value; with
  the scanner registered, or with neither variable set, it logs nothing.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

import pytest

from ream import comments, plugin_api, scanners, serve, settings, store
from ream.scan import Finding

PAGE = "<!doctype html><title>t</title><p>caf&eacute; &#x3c;b&#x3e; ok</p>"


@pytest.fixture()
def house(monkeypatch):
    monkeypatch.setattr(scanners, "_SCANNERS", [])
    monkeypatch.setattr(plugin_api, "_failures", {})
    return plugin_api.PluginAPI("house")


def _boom(text):
    raise RuntimeError("down")


def _clean(text):
    return []


def _hang(text):
    time.sleep(2)
    return []


def _stamped(db, comment_id):
    with db.connect() as conn:
        return conn.execute(
            "select gg_scanned_at is not null from comment where id = %s",
            (comment_id,),
        ).fetchone()[0]


def _agent_comment(db, page, body="fine", **kw):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body=body,
        **kw,
    )


def _asked(house, name="gitguardian", answer=None, **kw):
    """Register a scanner that records what it is asked and answers `answer`."""
    seen: list[str] = []

    def scan(text):
        seen.append(text)
        return [] if answer is None else answer

    house.scanner(name, scan, **kw)
    return seen


# --- a publish ------------------------------------------------------------------


def test_a_finding_refuses_a_publish_and_the_scanner_was_asked_once(db, house):
    seen = _asked(house, answer=[Finding("ggshield_aws_keys", 3)])
    with pytest.raises(store.SecretFound) as caught:
        store.publish(db, "seat", "t", PAGE)
    assert [f.shape for f in caught.value.findings] == ["ggshield_aws_keys"]
    assert seen == [PAGE]  # the body as submitted, and once


def test_a_clean_publish_asks_the_scanner_once(db, house):
    seen = _asked(house)
    store.publish(db, "seat", "t", PAGE)
    assert seen == [PAGE]


def test_a_preview_and_an_update_are_asked_once_each(db, house):
    seen = _asked(house)
    store.check_body(PAGE, agent="seat")
    assert seen == [PAGE]
    page = store.publish(db, "seat", "t", PAGE)
    seen.clear()
    store.patch(db, "seat", page.id, PAGE + "<p>more</p>", page.revision)
    assert seen == [PAGE + "<p>more</p>"]


def test_a_gitguardian_field_is_asked_and_a_plain_one_is_not(house):
    seen = _asked(house)
    store._check_secrets("plain", "title", agent="seat", artifact_id=None)
    assert seen == []
    store._check_secrets(
        "a note", "note", agent="seat", artifact_id=None, gitguardian=True
    )
    assert seen == ["a note"]


def test_a_local_finding_never_reaches_the_scanner(db, house):
    seen = _asked(house)
    with pytest.raises(store.SecretFound):
        store.publish(db, "seat", "t", PAGE + "<p>AKIA" + "IOSFODNN7EXAMPLE</p>")
    assert seen == []


def test_a_gitguardian_that_gives_no_opinion_lets_the_page_publish(db, house):
    house.scanner("gitguardian", _boom, on_error="skip")
    store.publish(db, "seat", "t", PAGE)
    assert plugin_api.failure_counts() == {"house:scanner": 1}


def test_a_blocking_gitguardian_that_fails_fails_the_publish(db, house):
    house.scanner("gitguardian", _boom, on_error="block")
    with pytest.raises(store.ScanFailed):
        store.publish(db, "seat", "t", PAGE)


def test_a_publish_that_could_not_be_second_scanned_blames_no_relay(db, house, caplog):
    """The old `ggshield_unavailable` line said core's own relay was asked and did not answer.
    Core has no relay now; the plug-in counts and logs its own fault."""
    house.scanner("gitguardian", _boom, on_error="skip")
    logger = logging.getLogger("ream.refusals")
    logger.addHandler(caplog.handler)
    try:
        store.publish(db, "seat", "t", PAGE)
    finally:
        logger.removeHandler(caplog.handler)
    assert not any("ggshield_unavailable" in r.getMessage() for r in caplog.records)


# --- an agent comment's second scan ---------------------------------------------


def test_a_clean_answer_stamps_the_comment_and_the_scanner_was_asked_once(db, house):
    seen = _asked(house)
    page = store.publish(db, "seat", "t", PAGE)
    seen.clear()
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "scanned"
    assert _stamped(db, row.id)
    assert seen == ["fine\n"]  # the body and the quote, once


def test_a_finding_withdraws_the_comment(db, house):
    page = store.publish(db, "seat", "t", PAGE)  # before the scanner would refuse it
    seen = _asked(house, answer=[Finding("ggshield_aws_keys", 1)])
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "withdrawn"
    assert comments.list_for_artifact(db, page.id) == []
    assert seen == ["fine\n"]


@pytest.mark.parametrize("fails", ["raises", "times out"])
def test_a_gitguardian_that_raises_or_times_out_leaves_the_comment_unscanned(
    db, house, fails
):
    page = store.publish(db, "seat", "t", PAGE)
    if fails == "raises":
        house.scanner("gitguardian", _boom, on_error="skip")
    else:
        house.scanner("gitguardian", _hang, on_error="skip", timeout=0.05)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "unavailable"
    assert not _stamped(db, row.id)  # the sweeper asks again
    assert [c.id for c in comments.list_for_artifact(db, page.id)] == [row.id]
    assert plugin_api.failure_counts() == {"house:scanner": 1}


@pytest.mark.parametrize("fails", ["raises", "times out"])
def test_the_sweeper_asks_again_and_stamps_it_once_gitguardian_answers(
    db, house, fails
):
    page = store.publish(db, "seat", "t", PAGE)
    if fails == "raises":
        house.scanner("gitguardian", _boom, on_error="skip")
    else:
        house.scanner("gitguardian", _hang, on_error="skip", timeout=0.05)
    row = _agent_comment(db, page)
    with db.connect() as conn:
        conn.execute(
            "update comment set created_at = %s where id = %s",
            (datetime.now(UTC) - timedelta(seconds=120), row.id),
        )
    assert comments.sweep_unscanned(db) == 0
    assert not _stamped(db, row.id)
    scanners._SCANNERS[:] = []
    house.scanner("gitguardian", _clean)
    assert comments.sweep_unscanned(db) == 1
    assert _stamped(db, row.id)


@pytest.mark.parametrize("fails", ["raises", "times out"])
def test_another_scanners_clean_answer_does_not_stand_in_for_gitguardian(
    db, house, fails
):
    page = store.publish(db, "seat", "t", PAGE)
    if fails == "raises":
        house.scanner("gitguardian", _boom, on_error="skip")
    else:
        house.scanner("gitguardian", _hang, on_error="skip", timeout=0.05)
    house.scanner("acme-secrets", _clean)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "unavailable"
    assert not _stamped(db, row.id)
    assert comments.list_for_artifact(db, page.id) != []  # skip: still shown


def test_a_clean_gitguardian_answer_clears_a_comment_by_itself(db, house):
    house.scanner("gitguardian", _clean)
    house.scanner("acme-secrets", _boom, on_error="skip")  # the other one is down
    page = store.publish(db, "seat", "t", PAGE)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "scanned"
    assert _stamped(db, row.id)


def test_another_scanners_finding_withdraws_while_gitguardian_is_down(db, house):
    page = store.publish(db, "seat", "t", PAGE)
    house.scanner("gitguardian", _boom, on_error="skip")
    house.scanner("acme-secrets", lambda text: [Finding("acme_token", 1)])
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "withdrawn"


def test_a_blocking_gitguardian_that_fails_holds_the_comment(db, house):
    page = store.publish(db, "seat", "t", PAGE)
    house.scanner("gitguardian", _boom, on_error="block")
    house.scanner("acme-secrets", _clean)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "unavailable"
    assert not _stamped(db, row.id)
    assert comments.list_for_artifact(db, page.id) == []  # held, reversibly


def test_without_gitguardian_another_scanners_clean_answer_counts(db, house):
    house.scanner("acme-secrets", _clean)
    page = store.publish(db, "seat", "t", PAGE)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "scanned"
    assert _stamped(db, row.id)


def test_with_no_scanner_at_all_a_comment_is_shown_unscanned(db, house):
    page = store.publish(db, "seat", "t", PAGE)
    row = _agent_comment(db, page)
    assert comments.second_scan(db, row.id) == "unavailable"
    assert not _stamped(db, row.id)
    assert [c.id for c in comments.list_for_artifact(db, page.id)] == [row.id]


# --- the bound and the settings ---------------------------------------------------


def test_the_plugins_twenty_second_bound_is_allowed(house):
    """The plug-in registers `gitguardian` with `timeout=20`; the relay always had 20 s."""
    house.scanner("gitguardian", _clean, on_error="skip", timeout=20.0)
    assert [s.bound() for s in scanners._SCANNERS] == [20.0]
    assert scanners.MAX_SCAN_TIMEOUT_SECONDS == 20.0


def test_a_deployment_that_still_sets_the_old_relay_variables_starts(monkeypatch):
    """Core reads no relay setting now: the plug-in reads `GGSHIELD_URL` and `GGSHIELD_TOKEN`
    itself. A deployment that still sets them (under either prefix, even differing) must not
    stop core, and core must not act on them."""
    for name in (
        "REAM_GGSHIELD_URL",
        "ARTIFACTS_GGSHIELD_URL",
        "REAM_GGSHIELD_TOKEN",
        "ARTIFACTS_GGSHIELD_TOKEN",
    ):
        monkeypatch.setenv(name, name.lower())
    settings.current()  # no EnvConflict, no unknown-variable refusal
    assert not hasattr(settings.current(), "ggshield_url")
    assert not hasattr(settings.current(), "ggshield_token")


# --- a start that has lost the second scan says so -------------------------------

_RELAY_VARIABLES = (
    "REAM_GGSHIELD_URL",
    "ARTIFACTS_GGSHIELD_URL",
    "REAM_GGSHIELD_TOKEN",
    "ARTIFACTS_GGSHIELD_TOKEN",
)
_SENTINEL = "relay-sentinel-0123"


@pytest.fixture()
def no_relay_variables(monkeypatch):
    for name in _RELAY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _start_warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "ream.serve" and r.levelno == logging.WARNING
    ]


def test_relay_variables_set_and_no_gitguardian_scanner_warns_once_naming_no_value(
    no_relay_variables, house, caplog
):
    no_relay_variables.setenv("REAM_GGSHIELD_URL", _SENTINEL)
    no_relay_variables.setenv("ARTIFACTS_GGSHIELD_TOKEN", _SENTINEL)
    with caplog.at_level(logging.WARNING, logger="ream.serve"):
        serve._warn_second_scan_off()
    (record,) = _start_warnings(caplog)
    text = record.getMessage()
    assert "REAM_GGSHIELD_URL" in text and "ARTIFACTS_GGSHIELD_TOKEN" in text
    assert "REAM_GGSHIELD_TOKEN" not in text  # only the ones that are set
    assert "OFF" in text and "gitguardian" in text
    assert _SENTINEL not in caplog.text


def test_each_relay_variable_alone_is_enough_to_warn(no_relay_variables, house, caplog):
    for name in _RELAY_VARIABLES:
        caplog.clear()
        no_relay_variables.setenv(name, _SENTINEL)
        with caplog.at_level(logging.WARNING, logger="ream.serve"):
            serve._warn_second_scan_off()
        (record,) = _start_warnings(caplog)
        assert name in record.getMessage()
        assert _SENTINEL not in caplog.text
        no_relay_variables.delenv(name)


def test_relay_variables_set_with_a_gitguardian_scanner_registered_is_silent(
    no_relay_variables, house, caplog
):
    no_relay_variables.setenv("REAM_GGSHIELD_URL", _SENTINEL)
    no_relay_variables.setenv("REAM_GGSHIELD_TOKEN", _SENTINEL)
    house.scanner("gitguardian", _clean, on_error="skip", timeout=20.0)
    with caplog.at_level(logging.WARNING, logger="ream.serve"):
        serve._warn_second_scan_off()
    assert _start_warnings(caplog) == []


def test_another_scanner_does_not_silence_the_warning(
    no_relay_variables, house, caplog
):
    no_relay_variables.setenv("REAM_GGSHIELD_URL", _SENTINEL)
    house.scanner("other", _clean)
    with caplog.at_level(logging.WARNING, logger="ream.serve"):
        serve._warn_second_scan_off()
    assert len(_start_warnings(caplog)) == 1


def test_no_relay_variable_set_is_silent_with_or_without_a_scanner(
    no_relay_variables, house, caplog
):
    no_relay_variables.setenv("REAM_GGSHIELD_URL", "   ")  # blank is unset
    with caplog.at_level(logging.WARNING, logger="ream.serve"):
        serve._warn_second_scan_off()
        house.scanner("gitguardian", _clean)
        serve._warn_second_scan_off()
    assert _start_warnings(caplog) == []


def test_the_real_entrypoint_warns_once_when_the_second_scan_is_off(
    db, house, no_relay_variables, caplog
):
    """Reached through `app_from_env`, after the plug-ins load (here none registers one)."""
    no_relay_variables.setenv("DATABASE_URL", db.dsn)
    no_relay_variables.setenv("REAM_MAINTENANCE", "off")
    no_relay_variables.setenv("REAM_GGSHIELD_TOKEN", _SENTINEL)
    with caplog.at_level(logging.WARNING, logger="ream.serve"):
        serve.app_from_env()
    ours = [r for r in _start_warnings(caplog) if "second scan" in r.getMessage()]
    assert len(ours) == 1
    assert "REAM_GGSHIELD_TOKEN" in ours[0].getMessage()
    assert _SENTINEL not in caplog.text
