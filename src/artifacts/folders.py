"""Folders: a page is in at most one folder, or none (docs/agent/specs/2026-11-02-folders-design.md).

A folder is a SHELF FACT like archive, pin and rename: filing a page makes no version, and an
identical republish still mints none. `folder.owner` is an agent name (an agent's own folder,
which holds that agent's pages only) or the reserved `jeremy` (his folder, which may hold any
agent's pages; slice 3). `artifact.agent` is never touched, so a page always shows its publisher.

Everything that writes takes an open connection, so the store's publish and patch file a page in
the same transaction that writes it. Only plain SQL both databases read the same.
"""

import re
from datetime import UTC, datetime

from artifacts import ids, settings

# Validated like agent names, shorter: a slug is typed on a command line every publish.
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def his() -> str:
    """The owner of a folder the reader makes: the configured owner id
    (`keys.reserved_agent_names`: no agent can be called this, so no agent key reaches these
    folders). It holds any agent's pages, each keeping its publisher."""
    return settings.current().owner_id


class BadSlug(Exception):
    """A folder slug that is not lowercase letters, digits and hyphens, 1-40 long."""

    field = "folder"


class SlugTaken(Exception):
    """A rename to a slug the owner already uses, as a folder or as another folder's old
    slug (409)."""

    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug


class FolderArchived(Exception):
    """A publish or move named an archived folder: unarchive it first (409)."""

    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug


def clean_slug(raw: str) -> str:
    """The slug itself, or `BadSlug`. Never lower-cases on the caller's behalf: a typo that is
    silently repaired is still a typo, and `Roadmap` becoming `roadmap` would hide it."""
    if not isinstance(raw, str) or not SLUG.fullmatch(raw):
        raise BadSlug(raw)
    return raw


def find(conn, owner: str, slug: str):
    """`(id, slug, title, archived_at)` of the folder `owner` calls `slug`, resolving an old
    slug through `folder_alias`; None when `owner` has no such folder."""
    row = conn.execute(
        "select id, slug, title, archived_at from folder where owner = %s and slug = %s",
        (owner, slug),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "select f.id, f.slug, f.title, f.archived_at from folder_alias a"
            " join folder f on f.id = a.folder_id"
            " where a.owner = %s and a.old_slug = %s",
            (owner, slug),
        ).fetchone()
    return row


def resolve_or_create(
    conn, owner: str, slug: str, title: str | None
) -> tuple[str, str, bool]:
    """`(folder id, canonical slug, created)`. The first use of an unknown slug creates the
    folder (`title` defaults to the slug and is set once, here); an archived folder refuses."""
    clean_slug(slug)
    row = find(conn, owner, slug)
    created = False
    if row is None:
        # `on conflict do nothing` rather than catching the violation: two publishes that name
        # a new folder at once both succeed, and exactly one of them says it created it.
        done = conn.execute(
            "insert into folder (id, owner, slug, title, created_at)"
            " values (%s, %s, %s, %s, %s) on conflict (owner, slug) do nothing",
            (ids.new_id(), owner, slug, title or slug, datetime.now(UTC)),
        )
        created = done.rowcount > 0
        row = find(conn, owner, slug)
    if row[3] is not None:
        raise FolderArchived(row[1])
    return row[0], row[1], created


def file_page(
    conn, artifact_id: str, owner: str, slug: str, title: str | None
) -> dict | None:
    """Put a page in `owner`'s folder `slug`, creating it on first use. `slug == ""` takes the
    page out. Returns what the API reports: `{"slug", "created"}`, or None for no folder."""
    if slug == "":
        conn.execute(
            "update artifact set folder_id = null, archived_via_folder = null"
            " where id = %s",
            (artifact_id,),
        )
        return None
    folder_id, canonical, created = resolve_or_create(conn, owner, slug, title)
    conn.execute(
        "update artifact set folder_id = %s, archived_via_folder = null where id = %s",
        (folder_id, artifact_id),
    )
    return {"slug": canonical, "created": created}


def rename_folder(
    conn, owner: str, slug: str, new_slug: str | None, title: str | None
) -> dict | None:
    """Give `owner`'s folder `slug` (an old slug resolves) a new slug and/or title. The old
    slug is kept as an alias, so every route and `--folder` still finds the folder by it.
    None when `owner` has no such folder; `SlugTaken` when `new_slug` is another folder's slug
    or alias of this owner. Renaming to the folder's own current slug changes nothing; renaming
    back to one of its own old slugs drops that alias. Never creates a folder."""
    if new_slug is not None:
        clean_slug(new_slug)
    row = find(conn, owner, slug)
    if row is None:
        return None
    folder_id, current = row[0], row[1]
    previous = None
    if new_slug is not None and new_slug != current:
        taken = conn.execute(
            "select 1 from folder where owner = %s and slug = %s", (owner, new_slug)
        ).fetchone()
        alias = conn.execute(
            "select folder_id from folder_alias where owner = %s and old_slug = %s",
            (owner, new_slug),
        ).fetchone()
        if taken is not None or (alias is not None and alias[0] != folder_id):
            raise SlugTaken(new_slug)
        if alias is not None:
            conn.execute(
                "delete from folder_alias where owner = %s and old_slug = %s",
                (owner, new_slug),
            )
        conn.execute("update folder set slug = %s where id = %s", (new_slug, folder_id))
        conn.execute(
            "insert into folder_alias (owner, old_slug, folder_id) values (%s, %s, %s)"
            " on conflict (owner, old_slug) do nothing",
            (owner, current, folder_id),
        )
        previous = current
        current = new_slug
    if title is not None:
        conn.execute("update folder set title = %s where id = %s", (title, folder_id))
    return {
        "slug": current,
        "title": conn.execute(
            "select title from folder where id = %s", (folder_id,)
        ).fetchone()[0],
        "previous_slug": previous,
    }


def list_for_agent(db, agent: str) -> list[dict]:
    """The calling agent's own folders: slug, title, page count (pages not in the bin), open
    questions (on live, unarchived pages: an archived page's questions are set aside, not
    waiting), and whether the folder is archived. Newest folder first. Only the owner's own pages count, so a
    folder's metadata never reveals another publisher's page."""
    with db.connect() as conn:
        rows = conn.execute(
            "select f.id, f.slug, f.title, f.archived_at, f.created_at,"
            " (select count(*) from artifact a"
            "   where a.folder_id = f.id and a.deleted_at is null and a.agent = f.owner),"
            " (select count(*) from decision d join artifact a on a.id = d.artifact_id"
            "   where a.folder_id = f.id and a.deleted_at is null and a.agent = f.owner"
            "   and a.archived_at is null"
            "   and d.kind <> 'form' and d.answer is null and d.closed_at is null)"
            " from folder f where f.owner = %s order by f.created_at desc, f.slug",
            (agent,),
        ).fetchall()
    return [
        {
            "slug": r[1],
            "title": r[2],
            "pages": r[5],
            "open_questions": r[6],
            "archived": r[3] is not None,
            "archived_at": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]


def page_ids(db, agent: str, slug: str) -> list[str] | None:
    """Ids of the pages in `agent`'s folder `slug`, newest change first; None when `agent` has
    no such folder. 'Not yours' and 'does not exist' are the same answer: a folder is named by
    its owner and slug together, so another agent's slug is simply not found."""
    with db.connect() as conn:
        row = find(conn, agent, slug)
        if row is None:
            return None
        return [
            r[0]
            for r in conn.execute(
                "select id from artifact where folder_id = %s and deleted_at is null"
                " and agent = %s order by updated_at desc, id",
                (row[0], agent),
            ).fetchall()
        ]


def slug_from_title(title: str) -> str:
    """A valid slug for a name he typed: lowercase letters and digits, runs of anything else
    one hyphen, at most 40 characters; "folder" when nothing usable is left."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return slug or "folder"


def create_his(conn, title: str) -> tuple[str, str]:
    """`(folder id, slug)` of a NEW folder of his, named `title`. The slug comes from the title
    and takes a numeric suffix when he already has one by that name, so naming a second
    folder "Roadmap" makes another rather than silently reusing the first."""
    base = slug_from_title(title)
    # Two "New folder" clicks with one name can both see a free slug; the unique (owner, slug)
    # decides, and the loser takes the next suffix instead of a 500.
    for n in range(1, 101):
        suffix = "" if n == 1 else f"-{n}"
        slug = base[: 40 - len(suffix)].rstrip("-") + suffix
        if find(conn, his(), slug) is not None:
            continue
        folder_id = ids.new_id()
        done = conn.execute(
            "insert into folder (id, owner, slug, title, created_at)"
            " values (%s, %s, %s, %s, %s) on conflict (owner, slug) do nothing",
            (folder_id, his(), slug, title, datetime.now(UTC)),
        )
        if done.rowcount > 0:
            return folder_id, slug
    raise SlugTaken(base)


def get(conn, folder_id: str):
    """`(id, owner, slug, title, archived_at)` of a folder by its opaque id, or None."""
    return conn.execute(
        "select id, owner, slug, title, archived_at from folder where id = %s",
        (folder_id,),
    ).fetchone()
