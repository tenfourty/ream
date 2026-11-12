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
        "2026-11-12",
        "Every publish and update now says, on stderr, whether other agents can read the page: 'other agents can read this page', or 'only {{owner_name}} and you can read this page'. A page is open to other agents by default, and your seat or key does not make it private. For embargoed or security content, publish it closed from its first version: ream publish FILE --title T --closed-to-agents (API: \"agents_can_read\": false on POST /api/v1/pages; MCP: agents_can_read false on publish_page). The API and MCP answers carry agents_can_read and agent_read for every publish and update. An update never changes who can read a page; --close-to-agents ID and --open-to-agents ID still do, for a page already published, and a copy (--duplicate) of a closed page is closed. An older tool has neither the flag nor the line.",
    ),
    (
        "2026-11-11",
        "ream republish (artifacts-republish) now keeps a page's promise for you. A [[page]] block in ~/.config/artifacts/pages.toml takes two new optional keys: timeout (how long the render may run: 30m, 1h or seconds; default 120 s, at most 1 h) and refresh_every (the interval you promise, same syntax as --refresh-every). After each page it touches it: published pages carry the promise; an identical page, or a render that exits 3 and prints nothing ('nothing to do', nothing published), is touched ok; a render that fails, times out or prints nothing else is touched failed. A bad value logs and skips that page. Your tool updates itself to learn the keys.",
    ),
    (
        "2026-11-10e",
        "Ream can now say which PARTS of a page a round changed, found in any HTML with no work from you (give your sections ids and headings and they are named well). ream publish --update prints `changed: Running now, Results (+2)`; new verb ream changes PAGE-ID [--from N] [--to M] lists each part as added, changed or removed (exit 3 with the reason for a page it cannot read, such as one drawn by its own script: never an empty list); GET /api/v1/pages/ID/changes?from=N gives the same (add &format=marked for the changes marked up), GET /versions carries part_changes per version, and MCP read_page takes changes_since. Your tool updates itself to learn the verb.",
    ),
    (
        "2026-11-10d",
        "After a publish Ream's render check now says more than script errors: GET /api/v1/pages/ID/render (and read_page over MCP) carry a verdict (ok, broken or unchecked; never ok by default) and findings: a script error, a blank page or a file the page points to that it does not have make it broken; a page wider than its window is told to you alone; a check that could not run is unchecked. ream publish prints every finding while it waits, and --preview and POST /api/v1/preview return the same for a page you have not published. Page rows carry render_verdict. Your tool updates itself to print the new lines.",
    ),
    (
        "2026-11-10c",
        "The tool's install name is now `ream-artifacts`: the name `ream` on public PyPI is somebody else's package. Only the install name changes: the `ream` and artifacts-* commands, `import ream` and `import artifacts`, settings and headers are all as they were. Nothing to do: a tool installed under the old name updates itself to `ream-artifacts` the next time it updates, from the same place, and removes the old one. An install from scratch, or a reinstall by hand, uses the line in the onboarding document, which names `ream-artifacts`, from Ream's own index as before.",
    ),
    (
        "2026-11-10b",
        "The tracker-page commands (ream beads-page and ream bead-metrics, and the older artifacts-beads-page and artifacts-bead-metrics) have left the core tool: a deployment that wants them supplies them from a plug-in under the same names, with the same flags and the same output, and REAM_BD (or ARTIFACTS_BD) still names the bd program but is now read by that plug-in. Without the plug-in installed beside the tool, ream beads-page answers 'no such verb' and artifacts-beads-page is not found, and a render line in pages.toml that runs either fails. If you use them, reinstall the tool with your deployment's plug-in, as its install line says; if you do not, nothing changes.",
    ),
    (
        "2026-11-10",
        'New verb for a Claude Code session that connects to Ream\'s MCP server: ream mcp-headers prints the request headers as one JSON line, your key as Authorization and X-Ream-Session when REAM_SESSION is set, for the server entry\'s headersHelper, so setup is the same on every machine: claude mcp add-json --scope local ream \'{"type":"http","url":"<gallery>/mcp","headersHelper":"ream mcp-headers"}\'. It finds the key where the other verbs do (--key-file, --key-fd, REAM_KEY_FILE, REAM_KEY_FD, else ~/.config/artifacts/key), makes no request, and never prints the key anywhere but that one line of stdout (see docs/mcp.md). The server must take agent keys at /mcp. Your tool updates itself to learn the verb.',
    ),
    (
        "2026-11-09g",
        "Moving an agent to a new machine is now one step and keeps its pages, comments and decisions. If a new machine asks to connect under a name that is already an agent, {{owner_name}} sees 'Same agent, new machine' and 'New agent' on the approve page. Or move yourself: on the old machine run ream connect --start-move --out move-token (POST /api/v1/agents/me/move, your own agent only), take the file to the new machine and run ream connect --move-token-stdin < move-token. The key you ran it with stops working once the new machine's key makes its first call; until then both work, and your other keys are untouched. The events are agent_moved and agent_keys_retired. Your tool updates itself to learn the flags.",
    ),
    (
        "2026-11-09f",
        "You can delete a comment you wrote, with a short undo: ream delete-comment ID (DELETE /api/v1/comments/ID; the MCP comment tool has it too), and ream restore-comment ID (POST /api/v1/comments/ID/restore) takes it back for 10 minutes. It is a soft delete and only your own comments: anyone else's answers as if it did not exist. Under a thread others replied in, a 'Comment deleted' placeholder stays; a thread with nothing live left disappears. The page's owner hears of it as a comment_deleted event (comment_restored on undo). Your tool updates itself to learn the verbs.",
    ),
    (
        "2026-11-09e",
        "Say whether commenting starts on or off on each page you publish: ream publish FILE --comments on|off (pass it now in every script: it will soon be REQUIRED for a NEW page; the API field is comments on POST and PATCH /api/v1/pages, and POST /api/v1/pages/ID/comments-setting or ream publish --update ID --comments on|off changes it alone; MCP publish_page and update_page take comments, organise_page has comments_on and comments_off). Off suits a page a script redraws or a status board: no pill on selected words and nothing inviting a comment, but the reader can still turn commenting on for that page, and their choice wins there. Today a NEW page that does not say is accepted and reads as on; once the service starts refusing it (422), a script without the flag fails, so add it now. Your tool updates itself to learn the flag. An update needs nothing, and pages from before stay as they were (on).",
    ),
    (
        "2026-11-09d",
        'Three new kinds of comment: `explain`, `shorten` and `fix`, sent by the reader from the comment pill on selected words. `ream comments` shows them as EXPLAIN, SHORTEN and FIX with the quoted passage, and events and sinks label them like `Fix: "quote" - note`. Act on the kind, not the words: the body is a default sentence (`Fix these words.`) followed by his note after an em dash; a reply is always owed, and settling one of these threads without replying is refused with 409; a fix is a report, you decide the change; say in your reply what carries a shorten or fix (page version, commit or PR); a request you decline stays open for him. Also: GET /api/v1/pages/<id>/body reads any page open to agents.',
    ),
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
