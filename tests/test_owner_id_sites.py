"""Every read of the owner id (`settings.owner_id()`, `.owner_id` on a settings object, an import of `owner_id`) in `src/ream` is classified here (people design, slice 1;
docs/agent/specs/2026-11-24-people-design.md), the way `test_scan_call_sites.py` classifies the
scan's call sites. The owner is person #1; as more people can sign in, each call has to mean one
of three things, and a new call fails this test until someone says which:

* REQUEST: "the request's person". A gallery handler takes it from `people.current`; a call that
  still reads settings has no request to ask yet and moves when its caller is threaded (slice 2/3).
* PAGE_OWNER: the owner standing for the page's owner, or for the one person who answers (an MCP
  owner-only tool, a relayed or partner answer, the owner's own comment label). Step 4 of the
  design (page ownership) and slice 3 (per-person answers) revisit each.
* SEED: the deployment's own owner by setting (a CLI listing, a fallback for a stored record that
  names nobody). It stays the setting.

Keyed by (file, enclosing function) with how many calls it holds.
"""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "ream"

REQUEST = "request person"
PAGE_OWNER = "page owner (step 4)"
SEED = "deployment seed"
KINDS = {REQUEST, PAGE_OWNER, SEED}

# (file, enclosing function) -> (kind, number of calls)
SITES: dict[tuple[str, str], tuple[str, int]] = {
    ("admin.py", "cmd_list_apps"): (SEED, 1),
    ("comments.py", "reopen"): (REQUEST, 1),
    ("decisions.py", "record_elsewhere"): (PAGE_OWNER, 2),
    ("formanswer.py", "submit"): (REQUEST, 1),
    ("formanswer.py", "relay"): (PAGE_OWNER, 3),
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


def _writes_and_owner_only():
    tree = ast.parse((SRC / "gallery.py").read_text())
    routes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and ast.unparse(dec.func.value) == "router"
            ):
                routes.append(
                    (dec.func.attr, ast.unparse(dec.args[0]), ast.unparse(node.args))
                )
    return routes


# Routes that take no person, with why. None today: every write names its person.
NO_PERSON_WRITES: dict[str, str] = {}
# Reads that are the owner's alone (they show keys and consent), not just writes.
OWNER_ONLY_READS = {"/machines", "/connect"}


def test_every_gallery_write_names_its_person():
    routes = _writes_and_owner_only()
    assert any(m == "post" for m, _, _ in routes), "the walk is broken"
    for method, path, args in routes:
        if method in ("post", "put", "patch", "delete"):
            ok = "people.CURRENT" in args or "people.REQUIRE_OWNER" in args
            assert ok or path in NO_PERSON_WRITES, (
                f"{method.upper()} {path} takes neither people.CURRENT nor"
                " people.REQUIRE_OWNER: say whose act it is"
            )
        if method == "get" and path in OWNER_ONLY_READS:
            assert "people.REQUIRE_OWNER" in args, path
