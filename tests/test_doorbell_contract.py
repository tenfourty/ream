"""What the core owes a doorbell, now that it carries none (core-and-plugins step 9d, second half).

The doorbell (a push to one seat's inbox when the owner answers or comments) is the house
plug-in's after-commit sink `doorbell`; the core's own `nudge.py` and its `REAM_NUDGE_*`
settings are gone. What stays here is the core's half: the event an after-commit sink is
handed, through the real gallery route, carries his note, the letter and the recommendation
(homelab-873: an answer of "(a)" once reached the mailbox as a bare letter while the 244 words
qualifying it stayed in the database); a sink that fails never costs his answer; and the
start-up warning when the old variables are still set with no doorbell to read them.
"""

import importlib
import logging

import pytest
from fastapi.testclient import TestClient

from ream import decisions, serve, sinks, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"
_OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]
_SENTINEL = "doorbell-value-never-logged"


def _answer_through_the_gallery(db, monkeypatch, *, note, recommended=None, letter="a"):
    """The real path: he taps an option in the gallery."""
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    published = store.publish(db, "ops-seat", "Board", PAGE)
    row = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question="ship it?",
        options=_OPTIONS,
        recommended=recommended,
    )
    response = TestClient(create_gallery_app(db)).post(
        f"/d/{row.id}/answer",
        data={"letter": letter, "note": note},
        headers={"origin": store.gallery_origin(), "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    return published, response


def test_the_core_has_no_doorbell_of_its_own():
    with pytest.raises(ImportError):
        importlib.import_module("ream.nudge")
    assert "nudge" not in {s.name for s in sinks._builtin_sinks()}


def test_an_answer_hands_a_doorbell_his_note_letter_and_recommendation(
    db, monkeypatch, rings
):
    note = (
        "check all folders between the two backups, then remove anything "
        "that is already elsewhere - only keep what is left behind that is "
        "unique so we can do forensics on it"
    )
    published, response = _answer_through_the_gallery(
        db, monkeypatch, note=note, recommended="a", letter="b"
    )
    assert response.status_code == 303
    (ring,) = rings
    assert ring["kind"] == "decision_answered" and ring["agent"] == "ops-seat"
    assert ring["artifact_id"] == published.id and ring["title"] == "Board"
    assert ring["note"] == note  # byte for byte, in its own field
    assert note not in ring["summary"]
    assert ring["letter"] == "b" and ring["recommended"] == "a"
    assert ring["event_id"].isdigit()


def test_an_answer_without_a_recommendation_hands_none(db, monkeypatch, rings):
    _answer_through_the_gallery(db, monkeypatch, note="", letter="b")
    (ring,) = rings
    assert ring["recommended"] is None and ring["note"] == ""


def test_answering_still_redirects_303_while_the_doorbell_raises(
    db, monkeypatch, ring_sink
):
    def boom(**_kw):
        raise RuntimeError("inbox down")

    ring_sink(boom)
    before = sinks.late_failures()
    published, response = _answer_through_the_gallery(db, monkeypatch, note="go ahead")
    assert response.status_code == 303
    assert response.headers["location"] == f"/a/{published.id}"
    assert sinks.late_failures() == before + 1


def test_the_service_carries_no_telegram_capability_at_all():
    """Jeremy, 19 Sep 2026: "telegram is only for the ops agent, it is not for
    all agents." No artifacts-originated event can produce a Telegram message
    except by the operator deciding to relay it. Asserted as an ABSENCE across
    the whole package: the claim IS "this string appears nowhere that could send"."""
    import pathlib

    import ream

    root = pathlib.Path(ream.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            lowered = line.lower()
            if "tell_jeremy" in lowered or "api.telegram.org" in lowered:
                offenders.append(f"{path.name}:{number}")
    assert offenders == [], (
        "the service acquired a way to message Jeremy directly: " + ", ".join(offenders)
    )


# --- the start-up warning ------------------------------------------------------------


@pytest.fixture()
def no_doorbell_variables(monkeypatch):
    for suffix in ("NUDGE_URL", "NUDGE_AGENT", "NUDGE_TOKEN", "NUDGE_TIMEOUT"):
        monkeypatch.delenv(f"REAM_{suffix}", raising=False)
        monkeypatch.delenv(f"ARTIFACTS_{suffix}", raising=False)
    return monkeypatch


def _start_warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "ream.serve" and r.levelno == logging.WARNING
    ]


def test_doorbell_variables_set_and_no_doorbell_warns_once_naming_no_value(
    no_doorbell_variables, caplog
):
    no_doorbell_variables.setenv("REAM_NUDGE_URL", _SENTINEL)
    no_doorbell_variables.setenv("ARTIFACTS_NUDGE_TOKEN", _SENTINEL)
    with sinks.isolated(), caplog.at_level(logging.WARNING, logger="ream.serve"):
        serve._warn_doorbell_off()
    (record,) = _start_warnings(caplog)
    text = record.getMessage()
    assert "REAM_NUDGE_URL" in text and "ARTIFACTS_NUDGE_TOKEN" in text
    assert "REAM_NUDGE_AGENT" not in text  # only the ones that are set
    assert "OFF" in text
    assert _SENTINEL not in caplog.text


def test_doorbell_variables_set_with_a_doorbell_sink_is_silent(
    no_doorbell_variables, caplog
):
    no_doorbell_variables.setenv("REAM_NUDGE_URL", _SENTINEL)
    doorbell = sinks.Sink(
        "doorbell",
        ("decision_answered",),
        deliver=lambda event: None,
        delivery="after_commit",
    )
    with (
        sinks.isolated(doorbell),
        caplog.at_level(logging.WARNING, logger="ream.serve"),
    ):
        serve._warn_doorbell_off()
    assert _start_warnings(caplog) == []


def test_no_doorbell_variable_set_is_silent_without_a_doorbell(
    no_doorbell_variables, caplog
):
    no_doorbell_variables.setenv("REAM_NUDGE_URL", "   ")  # blank is unset
    with sinks.isolated(), caplog.at_level(logging.WARNING, logger="ream.serve"):
        serve._warn_doorbell_off()
    assert _start_warnings(caplog) == []
