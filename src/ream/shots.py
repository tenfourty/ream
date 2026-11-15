"""A screenshot of an agent's own page, on demand (runcove-uap8t, slice 3).

`take` is what every surface calls (the HTTP route today; MCP and the CLI later): it
reads the page's body as its own agent may (never as a reader of another agent's
page), takes the picture where a render would be taken, and keeps nothing.
Nothing is stored and nothing is cached (docs/decisions.md, 15 Nov 2026).

Where the picture is taken follows `thumbs.render_report` exactly: with a renderer
set (`REAM_RENDERER_URL`) the page goes to it (`renderwire.call_shot`) and no browser
starts here, whatever REAM_BROWSER says; a renderer with no usable answer is a 503,
never a picture taken here instead. With none set, the browser runs in this process
(`rendercore.shoot`), inside the render's envelope and its one slot.

A refusal is a `ShotRefused` (status, detail, headers) the surface turns into its own
answer, except busy, which is `ShotBusy`: a `scanslots.ScanBusy`, so the app's and
MCP's existing busy handling answers it (503 with Retry-After).
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

from ream import rendercore, renderwire, scanslots, settings, stamp, store

log = logging.getLogger("ream.shots")

NO_BROWSER = "this server has no browser: it cannot take screenshots"
BUSY = "the browser is busy rendering other pages: try again in a few seconds"
IN_FLIGHT = "you already have a screenshot being taken: wait for it, then try again"
UNAVAILABLE = "the renderer gave no usable answer: try again later"
FAILED = "the screenshot failed on the server's side"

# A page id is minted by `ids.new_id`; this refuses only what no page id can be, so
# a malformed id is a field error, while a well-formed one that is not yours or not
# there is the 404 every other route gives.
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_VERSION_RE = re.compile(r"[0-9]{1,9}")
_WIDTH_RE = re.compile(r"[0-9]{1,5}")

# How long an agent whose own shot is still running is told to wait.
IN_FLIGHT_RETRY_AFTER_SECONDS = rendercore.SHOT_SLOT_WAIT_SECONDS


class ShotBusy(scanslots.ScanBusy):
    """Another render held the browser past the wait: 503 with Retry-After."""

    retry_after = rendercore.SHOT_SLOT_WAIT_SECONDS

    def __init__(self) -> None:
        Exception.__init__(self, BUSY)


class ShotRefused(Exception):
    """No picture: `status` and `detail` are the answer; `headers` go with it."""

    def __init__(self, status: int, detail, headers: dict[str, str] | None = None):
        super().__init__(status)
        self.status = status
        self.detail = detail
        self.headers = headers


def request_from(
    artifact_id: str,
    version: str | None,
    element: str | None,
    section: str | None,
    width: str | int | None,
    theme: str | None,
) -> tuple[int | None, renderwire.ShotRequest]:
    """The checked (version, `ShotRequest`) for these parameters as a surface got
    them (text, or None when not given), or ShotRefused 422 naming the field. Checked
    before anything is read, so a bad request answers the same for every page."""
    if not isinstance(artifact_id, str) or not ID_RE.fullmatch(artifact_id):
        raise ShotRefused(422, {"field": "id", "detail": "id is not a page id"})
    n = None
    if version is not None:
        text = str(version)
        if not _VERSION_RE.fullmatch(text) or int(text) < 1:
            raise ShotRefused(
                422, {"field": "version", "detail": "version is not a version number"}
            )
        n = int(text)
    if width is None:
        width = 1280
    if not isinstance(width, int):
        if not isinstance(width, str) or not _WIDTH_RE.fullmatch(width):
            raise ShotRefused(
                422,
                {"field": "width", "detail": "width is not one a shot is taken at"},
            )
        width = int(width)
    try:
        request = renderwire.shot_request(
            width, "light" if theme is None else theme, element, section
        )
    except renderwire.ShotRequestInvalid as exc:
        raise ShotRefused(422, {"field": exc.field, "detail": str(exc)}) from None
    return n, request


_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


@contextlib.contextmanager
def one_per_agent(agent: str) -> Iterator[None]:
    """Hold this agent's one shot in flight for the block, or ShotRefused 429."""
    with _in_flight_lock:
        if agent in _in_flight:
            raise ShotRefused(
                429,
                IN_FLIGHT,
                {"retry-after": str(IN_FLIGHT_RETRY_AFTER_SECONDS)},
            )
        _in_flight.add(agent)
    try:
        yield
    finally:
        with _in_flight_lock:
            _in_flight.discard(agent)


def take(
    db,
    agent: str,
    artifact_id: str,
    version: int | None,
    request: renderwire.ShotRequest,
) -> tuple[int, renderwire.Shot]:
    """(the version shot, the `Shot`, whose outcome is `shot`) of `agent`'s own page,
    or ShotRefused / ShotBusy. "Not yours" answers exactly as "not there"."""
    try:
        if version is None:
            stored = store.get_body(db, agent, artifact_id)
        else:
            stored = store.get_version_body(db, agent, artifact_id, version)
    except store.NotFound:
        raise ShotRefused(
            404, "no such page" if version is None else "no such page or version"
        ) from None
    url = settings.current().renderer_url
    binary = None
    if not url:
        binary = rendercore.browser_path()
        if binary is None:
            raise ShotRefused(501, {"renderer": False, "detail": NO_BROWSER})
    if rendercore.too_big_to_render(stored.body):
        raise _page_too_big(stored.body)
    with one_per_agent(agent):
        # Stamped as a reader would see it, as a render is (`thumbs.render_report`).
        html = stamp.apply(stored.body, datetime.now(UTC))
        if url:
            try:
                shot = renderwire.call_shot(url, html, request)
            except renderwire.RenderUnavailable:
                log.warning("screenshot not taken: the renderer gave no usable answer")
                raise ShotRefused(
                    503,
                    {
                        "renderer": True,
                        "reason": renderwire.UNAVAILABLE,
                        "detail": UNAVAILABLE,
                    },
                ) from None
        else:
            shot = rendercore.shoot(binary, html, request)
    return stored.version, _taken(shot, request, stored.body)


def _page_too_big(body: str) -> ShotRefused:
    return ShotRefused(
        413,
        {
            "field": "page",
            "size": len(body.encode("utf-8", "surrogatepass")),
            "limit": rendercore.RENDER_MAX_BYTES,
            "detail": "the page is too large to render",
        },
    )


def _taken(
    shot: renderwire.Shot, request: renderwire.ShotRequest, body: str
) -> renderwire.Shot:
    """`shot` if it is a picture, else the refusal its outcome stands for."""
    outcome = shot.outcome
    if outcome == "shot":
        return shot
    named = (
        "element"
        if request.element is not None
        else "section"
        if request.section is not None
        else None
    )
    if outcome == "no_match":
        raise ShotRefused(
            422,
            {
                "field": named,
                "detail": f"no part of the page is that {named}",
                "matches": 0,
            },
        )
    if outcome == "ambiguous":
        raise ShotRefused(
            422,
            {
                "field": named,
                "detail": f"more than one part of the page is that {named}",
                "matches": shot.matches,
            },
        )
    if outcome == "empty":
        raise ShotRefused(
            422,
            {
                "field": named or "page",
                "detail": f"the {named or 'page'} is drawn with no size",
            },
        )
    if outcome == "page_too_big":
        raise _page_too_big(body)
    if outcome == "picture_too_big":
        raise ShotRefused(
            413,
            {
                "field": "picture",
                "limit": renderwire.SHOT_PNG_MAX_BYTES,
                "detail": "the picture is too large: name an element or a section",
            },
        )
    if outcome == "timeout":
        raise ShotRefused(
            422,
            {
                "field": "page",
                "reason": "timeout",
                "detail": "the page took too long to render",
            },
        )
    if outcome == "memory":
        raise ShotRefused(
            422,
            {
                "field": "page",
                "reason": "memory",
                "detail": "the page used more memory than a render may",
            },
        )
    if outcome == "busy":
        raise ShotBusy()
    if outcome == "no_browser":
        raise ShotRefused(501, {"renderer": False, "detail": NO_BROWSER})
    raise ShotRefused(500, FAILED)
