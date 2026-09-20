"""Not a test: a one-shot that renders the real wall to a file so I can look at
it. Skipped unless ARTIFACTS_RENDER_TO is set, so it never runs in CI."""
import os
import pathlib

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, store
from artifacts.app import create_gallery_app

OUT = os.environ.get("ARTIFACTS_RENDER_TO")

# A 1x1 PNG. The wall only asks whether a thumbnail EXISTS, so this is enough
# to put a tile on the "has a picture" side of the mixture.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.mark.skipif(not OUT, reason="set ARTIFACTS_RENDER_TO to render the wall")
def test_render_the_wall(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Europe/Paris")

    def page(title, standfirst):
        return (
            f"<!doctype html><html><head><title>{title}</title></head><body>"
            f'<p class="standfirst">{standfirst}</p></body></html>'
        )

    rows = [
        ("warpgate-arc", "Warpgate: the gate Jeremy comes through",
         "Where the reverse proxy arc stands, and what is still waiting on him."),
        ("homelab-ops", "Homelab status",
         "Every node, every service, and the three that are not happy today."),
        ("artifacts-agent", "Artifacts: the page-hosting service",
         "What this service does, what shipped today, and what is still open."),
        ("cove-seat", "Forty-One Days on Cove",
         "A write-up of the first six weeks, for reading rather than scanning."),
        ("backup-arc", "Photo library, going off-site",
         "The upload is 21 % done and finishes Tuesday at the current rate."),
        ("seat-six", "Template — project status",
         "A worked example other agents copy. Everything here is placeholder."),
    ]
    ids = [store.publish(db, a, t, page(t, s)).id for a, t, s in rows]
    # A MIXTURE of pages with and without a picture, because that is what the
    # live wall is and a uniform fixture hid a ragged grid for a whole slice.
    for artifact_id in ids[:2]:
        store.save_thumbnail(db, artifact_id, 1, _PNG)
    store.set_pinned(db, ids[2], True, agent=None)
    for q in ("Which gate?", "And the certificate?"):
        decisions.ask(
            db, agent="warpgate-arc", artifact_id=ids[0], question=q,
            options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
            recommended="a",
        )
    store.set_archived(db, ids[5], True, agent=None)

    client = TestClient(create_gallery_app(db=db))
    html = client.get("/" + os.environ.get("ARTIFACTS_RENDER_QUERY", "")).text
    pathlib.Path(OUT).write_text(html)
    print(f"\nwrote {OUT} ({len(html)} bytes)")
