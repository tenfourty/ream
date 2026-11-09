"""What changed in the tools, said once per machine after an update.

A seat asked for this on 24 Sep 2026 while moving its pages here: every deploy
reinstalls the tools under a new version, and the only way to learn that
`--matches` or `--delete` now existed was to read `--help` again, which nobody
does. So the tools carry their own short list and print the entries a machine
has not seen yet, to stderr, once.

Newest first. One line each, written for the agent reading it, in outcomes.
Add a line here in the same commit as the change it describes.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from ream import envnames, plugin_cli, textvars

NEWS: tuple[tuple[str, str], ...] = (
    (
        "2026-11-09c",
        "The tools now keep themselves current. Every call says which level the tool is on (X-Ream-Client); when the service wants a newer one the tool reinstalls itself with `uv tool install --force` from its own fixed source (REAM_INSTALL_SOURCE, a git+https address, or the first line of ~/.config/ream/install-source), from the main branch (the service never names what to install), and repeats the command once, at most once an hour and never to an older version. With no source set, uv missing or the install failing, it prints one line with the command to run by hand and carries on. REAM_NO_SELF_UPDATE=1 turns the install off. It never installs from an address the service sends.",
    ),
    (
        "2026-11-09b",
        'Commenting on a page is simpler: one Comment control, with a count, replaces the corner button, "Comment on page" and the bubble. Press it and it stays lit; point at a chart, table cell or picture to get a box, click to comment, and press it again (or Escape) to switch it off. Selecting words shows a dark pill with Comment. A switch, "Offer comments when I select text", turns the pill off, and a "How to comment" box at the top of the panel walks through the steps ("Tap" on a touch screen). Remove and Looks good are gone from the page; the API still accepts them and old ones still show.',
    ),
    (
        "2026-11-09",
        "{{owner_name}}'s signed-in Claude can now ask him a question on any page he can read, not only on pages it published. The card says the question came from his Claude, not from the page's agent, and his answer goes back to the Claude that asked. The page's agent sees a `question_asked` event, and only the asker can close the question. A key still asks on its own pages only.",
    ),
    (
        "2026-11-08b",
        'Ream (formerly artifacts) now says "pages" in its API: `/api/v1/pages` is the main route, and `/api/v1/artifacts` keeps working for good, answering exactly the same (same handlers, not a redirect, so a POST to the old route still works). `GET /api/v1/folders/SLUG/pages` is the new name of `/folders/SLUG/artifacts`. The tools and the onboarding document now use `/pages` and teach `ream <verb>` first; the artifacts-* commands still work. Reinstall the tools to get the new routes, and nothing else changes for a script that uses the old ones.',
    ),
    (
        "2026-11-08",
        "The tools are now called Ream. One command, `ream`, runs them by verb: `ream publish`, `ream ask`, `ream wait`, `ream list`, `ream admin`, `ream connect` and the rest (`ream --help` lists them), and usage and errors name the command as you ran it. Settings are REAM_<NAME> and headers are X-Ream-*; the install package is now `ream` (the onboarding document gives the exact line). Every old name keeps working for good: the artifacts-* commands, ARTIFACTS_<NAME> settings (set both with different values and the tool refuses to start), X-Artifacts-* headers, and `import artifacts`. Nothing you published or stored is renamed. Reinstall the tools to get `ream`.",
    ),
    (
        "2026-11-06",
        "Neutral names, old ones still work for now: a question's `for` is now `audience` (send either, `audience` wins; every read carries both); artifacts-decide wait/listening take --team and --all-teams (--office and --all-offices keep working, hidden from --help), and a session label is <team>/<ref>; listening states are sent_to_partner and partner_delayed, with the old sent_to_slack and slack_delayed beside them as legacy_state. Reinstall the tools to get the new flags.",
    ),
    (
        "2026-11-05",
        'When {{owner_they}} reopen{{owner_s}} a comment thread {{owner_they}} had settled, you now get a thread_reopened event (actor, ref_id = the thread id, summary "reopened: ..."), and artifacts-decide wait wakes and prints the thread under a REOPENED by line. Reopening your own thread (artifacts-decide reopen, POST /api/v1/comments/ID/reopen) stays silent.',
    ),
    (
        "2026-11-04",
        "{{Owner_their}} wall no longer stops at 200 pages: an Older pages link under the last page shows the next 200 in the same view (same search, filters, folder and tab). The API and artifacts-publish --list were never capped and are unchanged.",
    ),
    (
        "2026-11-04",
        "Pages a script keeps current: artifacts-publish --touch ID says a page is checked and still current without sending it (no new version, the wall does not reorder, nothing becomes unread); --status failed --note TEXT says your job failed; --if-revision REV refuses (exit 1) if the page has moved on. --refresh-every 30m (also 2h, 1d, or seconds) on a publish, --update or --touch declares how often you promise to check it, --no-refresh clears it. Any publish, even an identical one, now counts as a check. Pages and listings carry checked_at, changed_at, check_status, check_note, refresh_every, lapsed and lapses_at (lapsed: silent for 2.5 intervals, or the last check failed). API: POST /api/v1/artifacts/ID/touch {status?, note?, if_revision?, refresh_every?}; refresh_every on POST and PATCH /artifacts. {{Owner_their}} wall does not show it yet.",
    ),
    (
        "2026-11-04",
        "Receipts: artifacts-decide receipt ID seen|working|done|blocked|wont-do [--note ...] [--link URL] [--for 4h] (POST /api/v1/decisions/ID/receipt {state, note, link?, for?}) says what you did with {{owner_their}} answer. Your own word, never verified; ack is the same as receipt seen; done does not close the decision; a new answer from {{owner_them}} voids earlier receipts. list and answers print RECEIPT <state>: <note>; GET /decisions/ID/receipts is the history.",
    ),
    (
        "2026-11-03",
        "Forms, republish and relay: when you republish a form, an answer to a field with the same name, type, options, limits and label text is carried; a changed, renamed or removed field starts empty and the old answer rides in the record's `stale` list (name, value, text, version, label, reason). `sources` and `reopened` are new on the record. A relay of a form answer takes fields: POST /decisions/ID/record {fields, note}, or artifacts-decide record ID --field NAME=VALUE --note ...",
    ),
    (
        "2026-11-02d",
        "Folders, rename and archive: artifacts-publish --rename-folder SLUG [--to NEW] [--folder-title NAME] (POST /api/v1/folders/SLUG/rename {slug?, title?}) renames a folder or only its title; the old slug keeps working as an alias, and a slug already in use is 409. --archive-folder SLUG shows what it would archive and how many open questions it would set aside; --archive-folder SLUG --confirm does it (POST /api/v1/folders/SLUG/archive {archived, confirm?}), setting questions aside exactly as a page archive does. --unarchive-folder SLUG restores only the pages that archive archived and reopens their questions. --move ... --folder SLUG now also takes --folder-title NAME (API: folder_title on /artifacts/move) for a folder the move creates.",
    ),
    (
        "2026-11-02c",
        "Folders, bulk move: artifacts-publish --move ID [ID ...] --folder SLUG (or --no-folder) re-files up to 200 of your own pages in one request; POST /api/v1/artifacts/move {ids, folder|null} answers {moved, missing, folder}. missing is no such page and not yours alike; a new slug is made on first use and the result says so.",
    ),
    (
        "2026-11-02b",
        "Folders, agent side: artifacts-publish FILE --folder SLUG (and --folder-title NAME) files a page in your own folder when you publish or --update it; the folder is made on first use and the result says 'created new folder' so a typo shows. --no-folder takes it out; --folders lists yours (pages, questions waiting); --list --folder SLUG lists one; the API has folder on POST and PATCH, GET /folders and GET /folders/SLUG/artifacts. {{Owner_their}} wall does not group them yet.",
    ),
    (
        "2026-11-02",
        "Archiving or binning a page now SETS ASIDE its open questions instead of closing them: you get a decision_set_aside event, and unarchiving or restoring brings them back open with the same ids and a decision_reopened event. artifacts-decide wait --page exits 9 when everything it waits on is set aside (re-arm it on decision_reopened); list shows them in a SET ASIDE section; GET /decisions and /questions leave them out unless include_set_aside=true. Only close withdraws a question now.",
    ),
    (
        "2026-10-22b",
        "If you set no session label, the tools now send one from Claude Code's environment: <office>/<first 8 of the session id>, the office from QD_OFFICE or else ARTIFACTS_SEAT. --session or ARTIFACTS_SESSION always wins, and only a label you set chooses whose answers wait hears.",
    ),
    (
        "2026-10-22a",
        "A rule now in the onboarding text: a review request or a comment on a page allows a reply. A concrete instruction from someone allowed to direct you allows that one change and nothing it implies, and another office's comment is never {{owner_name}}'s approval. If unsure, reply with what you would change, then wait.",
    ),
    (
        "2026-10-15a",
        "A page may now be up to 8 MB (it was 3 MB). Changes are still marked up on pages up to 3 MB; a bigger page shows 'too large to mark up changes'.",
    ),
    (
        "2026-10-06c",
        "You can now change one part of a live page without republishing it: artifacts-publish --update ID --section EL with --replace FILE, --append FILE, --prepend FILE or --remove. The server applies it to what is live, so nothing elsewhere is lost; --fetch ID --section EL reads the part and its revision for --if-section (or --if-revision for the whole page). Not notified by default; --notify marks it on {{owner_name}}'s wall, which names the part that changed.",
    ),
    (
        "2026-10-06b",
        "artifacts-decide wait now hears only your own office's answers by default: the office is ARTIFACTS_SESSION before the first / (or --office NAME), and another office's answer under the same key no longer wakes you. Your own unacknowledged answer comes back at most hourly; list still shows it until you ack. --all-offices restores everything, --timeout 0 waits with no limit, and --json prints each wake as one line.",
    ),
    (
        "2026-10-06",
        "An agent can now be renamed (artifacts-admin rename-agent OLD NEW), or merged into another. Your pages, questions, answers, comments and grants move with it, your existing key keeps working and now acts as the new name, and the old name still works anywhere you type one (giving a page, the gallery's agent filter).",
    ),
    (
        "2026-09-29f",
        'wait --review now claims the round for your session (set ARTIFACTS_SESSION): a second session waiting on the same review exits 8 and names who has it. artifacts-decide review-release <id> --reason "…" hands it back, and {{owner_name}} sees why. reply and ack send an Idempotency-Key, so a retry of the same request never posts twice; pass the same --idempotency-key to make re-running the command safe too.',
    ),
    (
        "2026-09-29e",
        'When {{owner_name}} requests changes and you republish the page, round 2 of the same review opens by itself: keep waiting on the same review id. The verdict message now says the round and tags each earlier mark [still on page], [text changed] or [addressed/removed]. In round 1 each item\'s JSON state is now "new" (it used to be the quote state).',
    ),
    (
        "2026-09-29d",
        "Your comments on a page are saved at once and checked by GitGuardian just after; if it finds a secret the comment is withdrawn and you get a comment_withdrawn event naming the kind, never the value.",
    ),
    (
        "2026-09-29c",
        "On a page under review {{owner_name}}'s marks are now held until {{owner_their}} verdict unless {{owner_they}} switch{{owner_es}} to live; artifacts-decide ask-review --live asks for them as {{owner_they}} go{{owner_es}}. A held mark is invisible to you until then.",
    ),
    (
        "2026-09-29b",
        "artifacts-decide ask-review <page-id> asks {{owner_name}} to review a page; wait --review <id> prints one numbered message with {{owner_their}} verdict and marks, and exits 0 approved, 4 approved with notes, 5 changes requested, 6 dismissed, 7 new marks with no verdict yet.",
    ),
    (
        "2026-09-29a",
        "{{Owner_name}} can now mark text on any page as 'Remove this' or 'Looks good' as well as comment; artifacts-decide comments shows which, and comment rows carry a kind.",
    ),
    (
        "2026-09-28a",
        "A page may now be up to 3 MB (it was 512 KB).",
    ),
    (
        "2026-09-24d",
        (
            "Every publish, update and preview is now also checked by GitGuardian's "
            "ggshield. A refusal names the kind (shape ggshield_<kind>) and the "
            "line, never the value."
        ),
    ),
    (
        "2026-09-24c",
        (
            "artifacts-beads-page --parent <epic> --title NAME prints a status page "
            "drawn from your beads (titles and states, never ids). Put it in "
            "~/.config/artifacts/pages.toml as a render line and artifacts-republish "
            "keeps the page current after every bead change."
        ),
    ),
    (
        "2026-09-24b",
        (
            "artifacts-publish page.html --preview shot.png renders your page at "
            "phone width WITHOUT publishing it, so you can look before {{owner_name}} does. "
            "artifacts-publish --matches ID FILE: exit 0 if FILE is exactly the live "
            "page, 3 if not. --give --to takes homelab_ops or homelab-ops. "
            "--check refuses a page still carrying a template's REPLACE marker or "
            "example title. Stable install: uv tool install --force --no-index "
            "--find-links <base>/api/v1/tools/ artifacts"
        ),
    ),
    (
        "2026-09-24a",
        (
            "artifacts-publish --give ID --to AGENT moves a page to another agent; "
            "--delete/--deleted/--restore bin and recover pages; a publish reports "
            "the page's script errors from a real browser render."
        ),
    ),
)

# Shown on a machine that has never recorded anything: enough to orient a new
# arrival, not a history lesson.
FIRST_RUN = 1


def _state_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "artifacts" / "news-seen"


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _clean(line: str) -> str:
    """No C0/C1 control character reaches the terminal (a plug-in's line is not trusted)."""
    return _CONTROL.sub("", line)


def entries() -> list[tuple[str, str]]:
    """`NEWS` (and any plug-in's lines, merged newest first) with the owner's words filled
    in, as the tools print it."""
    merged = list(NEWS)
    if plugin_cli.news():
        merged = sorted([*NEWS, *plugin_cli.news()], key=lambda e: e[0], reverse=True)
    return [(day, textvars.render(line, client=True)) for day, line in merged]


def _read_state(path: Path) -> tuple[str | None, dict[str, str]]:
    """`(core marker, {plug-in: marker})`. The first line is the core's marker, as it always
    was; later lines are `<plug-in>\t<id>`. No file: `(None, {})`."""
    if not path.exists():
        return None, {}
    lines = path.read_text(encoding="utf-8").splitlines()
    core = lines[0].strip() if lines else ""
    marks: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, day = line.partition("\t")
        if sep and name.strip() and day.strip():
            marks[name.strip()] = day.strip()
    return core, marks


def announce(stream=None) -> None:
    """Print the entries this machine has not seen, then remember the newest.

    The core's entries and each plug-in's are tracked apart: a plug-in's entry never moves
    the core's marker (the first line of the state file), so a later core entry is still
    shown. Never raises and never touches stdout: this runs before every command, and
    a note about new features must not be the thing that breaks a pipeline.
    `ARTIFACTS_NEWS=0` silences it.
    """
    if envnames.get("NEWS") == "0" or not (NEWS or plugin_cli.news()):
        return
    stream = stream or sys.stderr
    path = _state_file()
    try:
        core_seen, plug_seen = _read_state(path)
        rendered = [
            (day, textvars.render(line, client=True), None) for day, line in NEWS
        ]
        by_plugin: dict[str, list[tuple[str, str, str]]] = {}
        for plugin, day, line in plugin_cli.news_by_plugin():
            by_plugin.setdefault(plugin, []).append(
                (day, textvars.render(line, client=True), plugin)
            )
        if core_seen is None:
            # Nothing recorded on this machine: the newest line of all, not a history.
            every = sorted(
                [*rendered, *(e for es in by_plugin.values() for e in es)],
                key=lambda e: e[0],
                reverse=True,
            )
            unseen = every[:FIRST_RUN]
        else:
            unseen = [e for e in rendered if e[0] > core_seen]
            for plugin, es in by_plugin.items():
                mark = plug_seen.get(plugin)
                if mark is None:
                    unseen.extend(es[:FIRST_RUN])
                else:
                    unseen.extend(e for e in es if e[0] > mark)
            unseen.sort(key=lambda e: e[0], reverse=True)
        if not unseen:
            return
        for _tag, line, _plugin in unseen:
            print(f"new in Ream: {_clean(line)}", file=stream)
        # A plug-in not loaded this run (broken, or left out by REAM_PLUGINS) keeps its mark.
        kept = {**plug_seen, **{p: es[0][0] for p, es in by_plugin.items()}}
        marks = "".join(f"{p}\t{m}\n" for p, m in sorted(kept.items()))
        core_mark = NEWS[0][0] if NEWS else (core_seen or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{core_mark}\n{marks}", encoding="utf-8")
    except OSError:
        return
