# artifacts

A self-hosted "artifacts" publisher for the homelab: agents publish an HTML
page over a keyed API and get back a stable URL.

## Development

```bash
uv run pytest        # run tests
uv run ruff check src tests   # lint
```

Requires Python 3.12 exactly (pinned in `.python-version`) — a later task's
test dependency (`pgserver`) has no wheels for 3.13+.
