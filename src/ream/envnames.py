"""Setting names, new and old. Standard library only: the command-line tools use it too.

Every setting is `REAM_<NAME>`. The old form `ARTIFACTS_<NAME>` is kept for good, so a unit
file, a seat's shell or a deployment written before the rename keeps working.

* only one of the two is set: that one is used;
* both are set to the same value: that value is used;
* both are set and DIFFER: the process refuses to start, naming both variables (never their
  values: a value may be a credential). A silent preference would turn a stale old variable
  left in a unit file into the wrong key or origin with no signal.

A blank variable is unset, as it always was.
"""

import os

NEW_PREFIX = "REAM_"
OLD_PREFIX = "ARTIFACTS_"


class EnvConflict(SystemExit):
    """Both forms of one setting are set, to different values. Carries the message, which
    names the two variables and no value."""


def names(suffix: str) -> tuple[str, str]:
    """The new and old variable names for `suffix`."""
    return NEW_PREFIX + suffix, OLD_PREFIX + suffix


def get(suffix: str) -> str | None:
    """The setting `suffix` from the environment, stripped, or None when neither form is set
    (or both are blank). Raises EnvConflict when both are set and differ."""
    new_name, old_name = names(suffix)
    new = os.environ.get(new_name, "").strip()
    old = os.environ.get(old_name, "").strip()
    if new and old and new != old:
        raise EnvConflict(
            f"{new_name} and {old_name} are both set and differ: unset one of them"
        )
    return new or old or None


def set_names(suffix: str) -> list[str]:
    """The variable names of `suffix` that are set to something non-blank, in new-then-old
    order. Presence only: no value is returned, and a pair that differs is not an error here
    (this answers "is it set at all", for a setting the server no longer reads)."""
    return [name for name in names(suffix) if os.environ.get(name, "").strip()]


def child_env(env: dict[str, str], suffix: str, value: str | None) -> None:
    """Set (or, with None, remove) both forms of `suffix` in `env`, a child's environment, so
    a child written against either name reads it."""
    for name in names(suffix):
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
