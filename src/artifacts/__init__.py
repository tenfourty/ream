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

    def __init__(self) -> None:
        self._real_specs: dict[str, ModuleSpec] = {}

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
        self._real_spec(fullname)
        return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec: ModuleSpec):
        return importlib.import_module(_NEW + spec.name[len(_OLD) :])

    def _real_spec(self, fullname: str) -> ModuleSpec | None:
        """The spec `ream.<x>` was really imported with. Once `artifacts.<x>` has been
        imported the import system overwrites `module.__spec__` with the alias spec, so
        the first real one seen is kept."""
        real = _NEW + fullname[len(_OLD) :]
        if real not in self._real_specs:
            found = importlib.util.find_spec(real)
            if found is None or found.name != real:
                return found
            self._real_specs[real] = found
        return self._real_specs[real]

    def exec_module(self, module) -> None:
        """Nothing to run: the module was executed under its real name. The import
        system has just set `module.__spec__` to the alias spec; put the real one back."""
        real = self._real_spec(_OLD + module.__name__[len(_NEW) :])
        if real is not None:
            module.__spec__ = real

    def _real_loader(self, fullname: str):
        real = self._real_spec(fullname)
        if real is None or real.loader is None:
            raise ImportError(f"no loader for {fullname}", name=fullname)
        return real.loader

    # `python -m artifacts.<x>` (runpy) asks the alias spec's loader for the code.
    def get_code(self, fullname: str):
        return self._real_loader(fullname).get_code(_NEW + fullname[len(_OLD) :])

    def get_source(self, fullname: str):
        return self._real_loader(fullname).get_source(_NEW + fullname[len(_OLD) :])

    def is_package(self, fullname: str) -> bool:
        return self._real_loader(fullname).is_package(_NEW + fullname[len(_OLD) :])


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

# `import artifacts` hands back the ream package itself.
sys.modules[__name__] = ream
