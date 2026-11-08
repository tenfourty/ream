"""The old import name, kept for good: `import artifacts` is `import ream`.

The package was renamed to `ream` (docs/decisions.md, "Ream inside the code too"). Callers
outside this repository still write `import artifacts`, `from artifacts.settings import ...`,
`artifacts.plugin_api`, or name `artifacts.client:run` as an entry point, and all of it keeps
working. Nothing here is a copy:

* `artifacts` itself becomes the `ream` module object (so `resources.files("artifacts")` finds
  the schema and the templates in `ream`);
* `artifacts.X` is the SAME module object as `ream.X`, found by the finder below, so
  `settings.override(...)` or a monkeypatch through either name acts on one module, never on
  two copies that each hold their own state.

This file reads no environment and holds no logic of the service.
"""

import importlib
import importlib.abc
import importlib.util
import sys
from importlib.machinery import ModuleSpec

import ream

_OLD = "artifacts"
_NEW = "ream"


class _AliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Answers `artifacts.<x>` with the already-imported `ream.<x>`."""

    def find_spec(self, fullname, path=None, target=None) -> ModuleSpec | None:
        if not fullname.startswith(_OLD + "."):
            return None
        real = _NEW + fullname[len(_OLD) :]
        try:
            found = importlib.util.find_spec(real)
        except ModuleNotFoundError:
            return None
        if found is None:
            return None
        return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec: ModuleSpec):
        return importlib.import_module(_NEW + spec.name[len(_OLD) :])

    def exec_module(self, module) -> None:
        """Nothing to run: the module was executed under its real name."""


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

# `import artifacts` hands back the ream package itself.
sys.modules[__name__] = ream
