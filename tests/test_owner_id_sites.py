"""Every read of the owner id (`settings.owner_id()`, `.owner_id` on a settings object, an import of `owner_id`) in `src/ream` is classified here (people design, slice 1;
docs/agent/specs/2026-11-24-people-design.md), the way `test_scan_call_sites.py` classifies the
scan's call sites. The owner is person #1; as more people can sign in, each call has to mean one
of three things, and a new call fails this test until someone says which:

* REQUEST: "the request's person". A gallery handler takes it from `people.current`; a call that
  still reads settings has no request to ask yet and moves when its caller is threaded (slice 2/3).
* PAGE_OWNER: the owner standing for the page's owner, or for the one person who answers (an MCP
  owner-only tool, a relayed or partner answer, the owner's own comment label). Step 4 of the
  design (page ownership) and slice 3b (agent-visible per-person answers) revisit each.
* SEED: the deployment's own owner by setting (a CLI listing, a fallback for a stored record that
  names nobody). It stays the setting.

Keyed by (file, enclosing function) with how many calls it holds. The walker counts reads of an
`owner_id` attribute, imports of `owner_id`, and calls of `owner_person()` (the owner id read from settings).
"""

import ast
import pathlib
import re

from ream import people

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "ream"

REQUEST = "request person"
PAGE_OWNER = "page owner (step 4)"
SEED = "deployment seed"
KINDS = {REQUEST, PAGE_OWNER, SEED}

# Slice 1 of the agents design (docs/agent/specs/2026-11-26-agents-and-watching-design.md) reviewed
# each PAGE_OWNER row below. The two that decided "who answers a question for the page" moved to
# `agents.page_owners`. The rest stay, for one reason each: they name the human who answered or
# wrote (an author or actor field compared with the owner, `mcp.comment_image_label`,
# `mcp_tools._comment_writer`/`_event_writer`, `partner.answered_body`, `oidc.owner_login`,
# `folders.his`) or record an answer that came in from outside as the owner's
# (`partner_in.*`, `readside.answer_here`); none of them asks whose agent a page belongs to.
# (file, enclosing function) -> (kind, number of calls)
SITES: dict[tuple[str, str], tuple[str, int]] = {
    ("admin.py", "cmd_list_apps"): (SEED, 1),
    # `people.owner_person()` calls: "is this person the deployment's owner", by setting.
    ("access.py", "reads_sql"): (SEED, 1),  # the owner reads every live page
    ("mcp_apps.py", "reach_of"): (SEED, 1),  # the owner's app reads as the owner
    ("people.py", "current"): (SEED, 1),  # sign-in off: the request is the owner's
    ("agents.py", "shared_owner"): (
        PAGE_OWNER,
        1,
    ),  # who owns a shared agent's pages (step 4 changes it)
    # The owner's app before any agent exists (the consent screen): the owner, until slice 2 gives
    # an app its own person.
    ("agents.py", "owner_app"): (SEED, 1),
    ("agents.py", "person_label"): (SEED, 1),  # the owner's shown name is `owner_name`
    ("comments.py", "reopen"): (REQUEST, 1),
    # Both answering predicates now ask `agents.page_owners`/`owner_sql` (no owner-id read of their own).
    # record_elsewhere: the `said_by` default moved to page_owners; what stays is the signed-in
    # app's person (the request's, as an app is the owner's until slice 2 gives it its own).
    ("decisions.py", "record_elsewhere"): (REQUEST, 1),
    ("formanswer.py", "submit"): (REQUEST, 1),
    # relay: the default relayer moved to page_owners; the app check stays (as record_elsewhere).
    ("formanswer.py", "relay"): (REQUEST, 1),
    ("formrecord.py", "source_of"): (SEED, 1),
    ("mcp.py", "comment_image_label"): (PAGE_OWNER, 1),
    ("mcp_tools.py", "_comment_writer"): (PAGE_OWNER, 1),
    ("mcp_tools.py", "_event_writer"): (PAGE_OWNER, 1),
    ("partner_in.py", "_apply_artifacts"): (PAGE_OWNER, 1),
    ("partner_in.py", "answer_here"): (PAGE_OWNER, 3),
    ("readside.py", "answer_here"): (PAGE_OWNER, 2),
    ("gallery.py", "_bubble"): (REQUEST, 1),
    # Attribute reads, not calls.
    ("folders.py", "his"): (
        PAGE_OWNER,
        1,
    ),  # the folder owner; the gallery's actor is person.id
    ("keys.py", "reserved_agent_names"): (SEED, 1),
    ("oidc.py", "owner_login"): (PAGE_OWNER, 1),
    ("partner.py", "answered_body"): (PAGE_OWNER, 1),
    ("textvars.py", "variables"): (SEED, 1),
    ("people.py", "owner_person"): (SEED, 1),
    ("people.py", "_person"): (SEED, 1),
    ("people.py", "fill_owner_identity"): (SEED, 1),
    ("people.py", "find_or_make"): (SEED, 1),
    ("serve.py", "app_from_env"): (SEED, 1),
}


def _found() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "settings.py":
            continue  # defines owner_id
        tree = ast.parse(path.read_text())
        parent = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            # Every read of an `owner_id` attribute (`settings.owner_id()`, `settings.current()
            # .owner_id`, `cfg.owner_id`) and every `from ... import owner_id`.
            # `people.owner_person()` is the owner id too, read from settings: a call of it counts.
            if isinstance(node, ast.Call) and (
                (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "owner_person"
                )
                or (isinstance(node.func, ast.Name) and node.func.id == "owner_person")
            ):
                up = node
                while up in parent and not isinstance(
                    up, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    up = parent[up]
                key = (path.name, getattr(up, "name", "?"))
                found[key] = found.get(key, 0) + 1
                continue
            if not (
                (isinstance(node, ast.Attribute) and node.attr == "owner_id")
                or (
                    isinstance(node, ast.ImportFrom)
                    and any(a.name == "owner_id" for a in node.names)
                )
            ):
                continue
            up = node
            while up in parent and not isinstance(
                up, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                up = parent[up]
            key = (path.name, getattr(up, "name", "?"))
            found[key] = found.get(key, 0) + 1
    return found


def test_every_owner_id_call_is_classified():
    found = _found()
    assert found, "the walk found no call: it is broken, not the code clean"
    assert {k: n for k, n in found.items()} == {k: v[1] for k, v in SITES.items()}, (
        "a settings.owner_id() call was added, moved or removed: say in SITES whether it"
        " is the request's person, the page owner (step 4) or the deployment seed"
    )
    assert {kind for kind, _ in SITES.values()} <= KINDS


def test_a_gallery_handler_takes_the_person_from_people_current():
    """The handlers that act for the request's person no longer read settings: the gallery's
    only remaining call is a render helper with no request to ask."""
    gallery = {k for k in _found() if k[0] == "gallery.py"}
    assert gallery == {("gallery.py", "_bubble")}


def _takes(dependant, call) -> bool:
    return any(d.call is call or _takes(d, call) for d in dependant.dependencies)


# Writes that take no person, one reason each. None today.
NO_PERSON_WRITES: dict[str, str] = {}
# Reads that are the owner's alone (they show keys and consent).
OWNER_ONLY_READS = {"/machines", "/connect"}
# Reads that write (mark a page read, count a view): they take the request's person.
READS_THAT_WRITE = {"/a/{artifact_id}", "/a/{artifact_id}/frame"}
# Reads that are a person's own (read marks, pins, the inbox's unseen replies): they take the
# request's person (people design, slice 2).
READS_BY_PERSON = {"/", "/f/{folder_id}", "/inbox"}


def test_every_gallery_route_that_writes_names_its_person(db):
    """Walks the real router (async and sync handlers, `add_api_route`, routes another module
    registers on it) and its dependency trees: a write names `people.current`, directly or
    through `require_owner`."""
    from fastapi.routing import APIRoute

    from ream.gallery import gallery_router

    routes = [r for r in gallery_router(db).routes if isinstance(r, APIRoute)]
    assert len(routes) > 40, "the walk is broken"
    seen_post_forms = False
    for route in routes:
        named = _takes(route.dependant, people.current)
        owner_only = _takes(route.dependant, people.require_owner)
        for method in route.methods:
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                seen_post_forms |= route.path == "/a/{artifact_id}/forms/{form}"
                assert named or route.path in NO_PERSON_WRITES, (
                    f"{method} {route.path} does not depend on people.current:"
                    " say whose act it is"
                )
            if method == "GET" and route.path in OWNER_ONLY_READS:
                assert owner_only, route.path
            if method == "GET" and route.path in READS_THAT_WRITE | READS_BY_PERSON:
                assert named, route.path
    assert seen_post_forms, "the forms route (forms_ui.register) is not in the walk"
    assert {r.path for r in routes} >= (
        OWNER_ONLY_READS | READS_THAT_WRITE | READS_BY_PERSON
    )


def test_no_gallery_route_takes_a_person_id_from_the_request(db):
    """A person is who the request's session says, never a parameter: no path, query, header,
    cookie, form or body parameter of any gallery route is named like one (people design,
    slice 4: 'no route takes a person id')."""
    from fastapi.routing import APIRoute

    from ream.gallery import gallery_router
    from ream.signin import signin_router

    routes = [
        r
        for router in (gallery_router(db), signin_router(db))
        for r in router.routes
        if isinstance(r, APIRoute)
    ]
    assert len(routes) > 40, "the walk is broken"
    named = set()
    for route in routes:
        dependant = route.dependant
        stack = [dependant]
        while stack:
            d = stack.pop()
            stack.extend(d.dependencies)
            for param in (
                d.path_params
                + d.query_params
                + d.header_params
                + d.cookie_params
                + d.body_params
            ):
                if re.search(r"person|actor|as_user|on_behalf", param.name):
                    named.add((route.path, param.name))
    assert named == set(), named
