"""`artifacts-decide`, driven against the real app over real HTTP.

homelab-ij8.24's acceptance criterion is "a seat on baba can pose a decision and
read the answer back using only the documented commands, with the key never
appearing on a command line or in a transcript", so that is what these check --
the round trip end to end, and the key's absence from everything the command
emits. Same live-uvicorn arrangement as test_client.py, for the same reason: the
point of this client is that it works with nothing but the standard library and
a socket.
"""


import pytest

# Reuses test_client's fixtures rather than restating them -- same app, same
# server, same key file. Importing the fixture functions is what makes them
# available here; pytest resolves them by name.
from test_client import key_file, live  # noqa: F401

from artifacts import decide, decisions, keys, store

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"


@pytest.fixture()
def page(db, key_file):  # noqa: F811
    """A page owned by the same seat the key names, so the scope lines up."""
    return store.publish(db, "ops-seat", "Board", PAGE)


def _run(args, key_path) -> int:
    return decide.main(["--key-file", str(key_path), *args])


# --- posing one -------------------------------------------------------------


def test_a_seat_poses_a_question_and_reads_it_back(live, key_file, page, capsys):  # noqa: F811
    path, _ = key_file
    assert (
        _run(
            [
                "ask", page.id,
                "--question", "Where should the renderer run?",
                "--option", "a=In the app pod",
                "--option", "b=Its own deployment",
            ],
            path,
        )
        == 0
    )
    posed = capsys.readouterr().out
    assert "Where should the renderer run?" in posed
    assert "(a) In the app pod <- recommended" in posed
    assert "unanswered" in posed

    assert _run(["answers", page.id], path) == 0
    assert "Where should the renderer run?" in capsys.readouterr().out


def test_the_first_option_is_the_recommendation_unless_told_otherwise(
    live, key_file, page, db, capsys  # noqa: F811
):
    """His house style, from CLAUDE.md: "recommendation first as (a), because
    that is how he answers." A question posed with no recommendation at all is
    one he has to reverse-engineer, so the common case must not require the
    flag."""
    path, _ = key_file
    _run(["ask", page.id, "--question", "Q?", "--option", "a=One", "--option", "b=Two"], path)
    capsys.readouterr()
    rows = decisions.list_for_artifact(db, page.id)
    assert rows[0].recommended == "a"

    _run(
        ["ask", page.id, "--question", "R?", "--option", "a=One", "--option",
         "b=Two", "--recommend", "b"],
        path,
    )
    capsys.readouterr()
    posed = [r for r in decisions.list_for_artifact(db, page.id) if r.question == "R?"]
    assert posed[0].recommended == "b", "an explicit --recommend still wins"


def test_an_option_without_a_label_is_refused_with_the_shape_to_use(
    live, key_file, page, capsys  # noqa: F811
):
    path, _ = key_file
    assert _run(["ask", page.id, "--question", "Q?", "--option", "a"], path) == 2
    err = capsys.readouterr().err
    assert "LETTER=LABEL" in err
    assert "--option a=" in err, "the error carries a usable example, not just a rule"


def test_a_label_may_contain_an_equals_sign():
    assert decide._options(["a=set x = 1"]) == [{"letter": "a", "label": "set x = 1"}]


def test_a_page_this_key_does_not_own_is_a_404_not_a_different_answer(
    live, key_file, db, capsys  # noqa: F811
):
    """The service answers "not yours" and "no such page" identically so a key
    cannot be walked to learn which ids are real. The CLI must not undo that by
    reporting them differently."""
    path, _ = key_file
    keys.mint(db, agent="another-seat")
    theirs = store.publish(db, "another-seat", "Not yours", PAGE)

    assert _run(["ask", theirs.id, "--question", "Q?", "--option", "a=One", "--option", "b=Two"], path) == 1
    mine_err = capsys.readouterr().err
    assert _run(["ask", "zzzzzzzzzzzzzzzzzzzzzzzzzz", "--question", "Q?",
                 "--option", "a=One", "--option", "b=Two"], path) == 1
    missing_err = capsys.readouterr().err
    assert "404" in mine_err and "404" in missing_err
    assert mine_err == missing_err, (
        "a different message for 'exists but not yours' lets a key enumerate ids"
    )


# --- reading his answer back ------------------------------------------------


def test_an_answered_decision_leads_with_the_answer_and_his_whole_note(
    live, key_file, page, db, capsys  # noqa: F811
):
    """The note is never truncated. Every note he has written so far qualified
    the letter it came with, and "start with (b) but then (a) if we can" cut to
    "start with (b)" is the exact fabrication this service exists to prevent."""
    path, _ = key_file
    asked = decisions.ask(
        db, agent="ops-seat", artifact_id=page.id, question="Which?",
        options=[{"letter": "a", "label": "First"}, {"letter": "b", "label": "Second"}],
        recommended="a",
    )
    note = "start with (b) but then (a) if we can, and tell me before you switch"
    decisions.answer(db, asked.id, letter="b", note=note, by="jeremy")

    assert _run(["answers", page.id], path) == 0
    out = capsys.readouterr().out
    assert "ANSWER (he tapped it): (b) Second" in out
    assert note in out, "his words in full"


def test_open_only_hides_the_answered_ones(live, key_file, page, db, capsys):  # noqa: F811
    path, _ = key_file
    answered = decisions.ask(
        db, agent="ops-seat", artifact_id=page.id, question="Answered one?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )
    decisions.answer(db, answered.id, letter="a", note="", by="jeremy")
    decisions.ask(
        db, agent="ops-seat", artifact_id=page.id, question="Still open?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )

    _run(["answers", page.id, "--open-only"], path)
    out = capsys.readouterr().out
    assert "Still open?" in out
    assert "Answered one?" not in out

    _run(["answers", page.id], path)
    both = capsys.readouterr().out
    assert "Still open?" in both and "Answered one?" in both


def test_nothing_to_report_says_so_rather_than_printing_nothing(
    live, key_file, page, capsys  # noqa: F811
):
    """An empty result and a broken call look identical when both print
    nothing. ij8.51 lost a shift to exactly that shape."""
    path, _ = key_file
    assert _run(["list"], path) == 0
    assert "nothing waiting on Jeremy" in capsys.readouterr().out

    assert _run(["answers", page.id], path) == 0
    assert "no questions on that page" in capsys.readouterr().out


def test_an_uncollected_answer_says_how_to_collect_it(
    live, key_file, page, db, capsys  # noqa: F811
):
    path, _ = key_file
    asked = decisions.ask(
        db, agent="ops-seat", artifact_id=page.id, question="Which?",
        options=[{"letter": "a", "label": "First"}, {"letter": "b", "label": "Second"}],
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="go", by="jeremy")

    _run(["answers", page.id], path)
    assert f"artifacts-decide ack {asked.id}" in capsys.readouterr().out

    assert _run(["ack", asked.id], path) == 0
    capsys.readouterr()
    _run(["answers", page.id], path)
    assert "NOT YET ACKNOWLEDGED" not in capsys.readouterr().out


# --- the relay, which is the one thing an agent may not do as him ------------


def test_a_relayed_answer_is_stored_as_a_relay_and_never_as_a_tap(
    live, key_file, page, db, capsys  # noqa: F811
):
    """Give the agent the action, never the disguise. The whole point of
    `record` is that the resulting row cannot be mistaken for something Jeremy
    pressed, so the output has to say so too."""
    path, _ = key_file
    asked = decisions.ask(
        db, agent="ops-seat", artifact_id=page.id, question="Which?",
        options=[{"letter": "a", "label": "First"}, {"letter": "b", "label": "Second"}],
        recommended="a",
    )
    assert (
        _run(
            ["record", asked.id, "--letter", "a", "--source", "telegram",
             "--note", "yes but only after the migration"],
            path,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "relayed from telegram" in out
    assert "he tapped it" not in out
    assert "yes but only after the migration" in out

    stored = decisions.list_for_artifact(db, page.id, include_answered=True)[0]
    assert stored.answer_source == "telegram"
    assert stored.recorded_by == "ops-seat", "the key names the seat, not the body"


def test_the_command_cannot_claim_he_tapped_it(live, key_file):  # noqa: F811
    """`--source page` is not offered at all -- argparse refuses it before a
    request exists. The service refuses it too; this is the near guard."""
    with pytest.raises(SystemExit):
        decide.build_parser().parse_args(
            ["record", "d1", "--letter", "a", "--note", "x", "--source", "page"]
        )


# --- the key never leaks ----------------------------------------------------


def test_the_key_appears_in_nothing_the_command_prints(
    live, key_file, page, db, capsys  # noqa: F811
):
    """ij8.24's acceptance criterion names this explicitly. Checked on the
    failure path as well as the happy one, because an error is where a naive
    client dumps the request it just made."""
    path, token = key_file
    _run(["ask", page.id, "--question", "Q?", "--option", "a=One",
          "--option", "b=Two"], path)
    _run(["answers", page.id], path)
    _run(["ack", "nosuchdecision"], path)
    _run(["record", "nosuchdecision", "--letter", "a", "--note", "x",
          "--source", "other"], path)
    seen = capsys.readouterr()
    assert token not in seen.out
    assert token not in seen.err
    assert "art_" not in seen.out and "art_" not in seen.err


def test_a_missing_key_file_is_a_usage_error_not_a_crash(live, tmp_path, capsys):  # noqa: F811
    assert decide.main(["--key-file", str(tmp_path / "nope"), "list"]) == 2
    assert "cannot read key file" in capsys.readouterr().err


# --- the stale-install hint, inherited ---------------------------------------


def test_an_unknown_flag_here_also_says_how_old_the_build_is(capsys):
    """Same parser as artifacts-publish, so the same answer to "does this flag
    not exist, or is my copy old?". Asserted rather than assumed: the sharing is
    an import, and an import is easy to undo by accident.

    Driven through a SUBcommand deliberately. Nearly every flag here is on one,
    and argparse builds subparsers as plain ArgumentParsers unless the parser
    class is passed down -- so this is the case that would actually regress."""
    with pytest.raises(SystemExit):
        decide.main(["list", "--no-such-flag"])
    err = capsys.readouterr().err
    assert "uv tool install" in err and decide.installed_stamp() in err


def test_a_withdrawn_question_does_not_read_as_one_still_waiting(
    live, key_file, page, db, capsys  # noqa: F811
):
    """`close` used to answer with the word "unanswered", which is true and
    reads as "nothing happened". Found by running the command against the live
    service, not by reading it -- the unit tests all went through `answers`,
    where a closed row is filtered out and never formatted."""
    path, _ = key_file
    asked = decisions.ask(
        db, agent="ops-seat", artifact_id=page.id, question="Never mind?",
        options=[{"letter": "a", "label": "One"}, {"letter": "b", "label": "Two"}],
        recommended="a",
    )
    assert _run(["close", asked.id], path) == 0
    out = capsys.readouterr().out
    assert "WITHDRAWN" in out
    assert "unanswered" not in out, "withdrawn and still-waiting must not look alike"


def test_a_question_withdrawn_with_its_page_is_still_readable(
    live, key_file, page, db, capsys  # noqa: F811
):
    """homelab-ij8.44, and this morning made it sharper.

    Jeremy ruled on 21 Sep that archiving or deleting a page withdraws its open
    questions. So most withdrawals are now the SERVICE\'s doing and the agent
    never chose them -- and a withdrawn question used to be unreadable on every
    route an agent could reach. Not wrong, not redacted: simply no longer
    listed. A seat would be told "your question is gone" by an event and then
    find no trace of it anywhere.
    """
    path, _ = key_file
    asked = decisions.ask(
        db, agent="ops-seat", artifact_id=page.id, question="Gone with the page?",
        options=[{"letter": "a", "label": "One"}, {"letter": "b", "label": "Two"}],
        recommended="a",
    )
    store.set_archived(db, page.id, True, agent=None)

    assert _run(["answers", page.id], path) == 0
    out = capsys.readouterr().out
    assert asked.id in out, "the withdrawn question vanished from its own page"
    assert "WITHDRAWN" in out, "and it must not look like one still waiting"


def test_open_only_still_leaves_a_withdrawn_question_out(
    live, key_file, page, db, capsys  # noqa: F811
):
    """Because it is not waiting on anybody. The two readings are different
    questions and get different answers."""
    path, _ = key_file
    decisions.ask(
        db, agent="ops-seat", artifact_id=page.id, question="Gone with the page?",
        options=[{"letter": "a", "label": "One"}, {"letter": "b", "label": "Two"}],
        recommended="a",
    )
    store.set_archived(db, page.id, True, agent=None)

    assert _run(["answers", page.id, "--open-only"], path) == 0
    assert "no questions on that page are still open" in capsys.readouterr().out


def test_a_global_flag_after_the_command_says_so_rather_than_blaming_the_install(
    capsys,
):
    """I walked into this myself four minutes after installing the tool.

    `artifacts-decide ask <id> ... --session X` is a POSITION error -- --session
    is declared on the top-level parser -- but argparse refuses it with the same
    "unrecognized arguments" wording an absent flag gets. The stale-install hint
    then told me to refresh an install I had just made, which is the diagnostic
    misdiagnosing, and the worst kind: confidently, at the one moment the reader
    is paying attention.
    """
    with pytest.raises(SystemExit):
        decide.main(["ask", "x", "--question", "q", "--option", "a=1",
                     "--session", "seat/1"])
    err = capsys.readouterr().err
    assert "goes BEFORE the command" in err
    assert "ARTIFACTS_SESSION" in err, "and names the way that avoids the trap"
    assert "uv tool install" not in err, (
        "it blamed the install for a flag that is present and merely misplaced"
    )


def test_the_hint_names_the_command_you_actually_ran(capsys):
    """It said "this copy of artifacts-publish" when run as artifacts-decide,
    because the message was a module constant in the other tool's file. Small,
    and exactly the kind of thing that makes a reader distrust the rest of it.
    """
    with pytest.raises(SystemExit):
        decide.main(["list", "--no-such-flag"])
    assert "this copy of artifacts-decide" in capsys.readouterr().err


def test_a_refused_cross_seat_check_does_not_look_like_nothing_stranded(
    live, key_file, page, db, capsys  # noqa: F811
):
    """Reported by the operator seat from a real terminal, 22 Sep 2026: it ran
    the stranded check across seats under a key without the grant, and got one
    line -- `HTTP 403` -- where rows would have been. Its words: a caller "sees
    no rows and can conclude nothing stranded".

    That is the failure this house has already paid for: a refusal parsed as
    zero rows and read as "he never answered". An emptiness claim and an
    authorisation failure must not look alike at a glance. So the refusal says
    REFUSED, says it is not an empty result, and carries the service's own
    explanation -- which names the seat the key authenticates as, because every
    session on the machine shares one key file and the seat is rarely the one
    the caller assumed.
    """
    path, _ = key_file  # ops-seat, which holds no grant

    rc = _run(["list", "--all", "--stranded"], path)

    out, err = capsys.readouterr()
    assert rc != 0, "a refusal must not exit 0"
    assert "no uncollected answers" not in out, "and must never print the empty line"
    assert "REFUSED" in err
    assert "not an empty result" in err.lower()
    assert "decisions:read-all" in err, "the service's own explanation, carried"
    assert "403" in err, "and the status, which callers grep for"
