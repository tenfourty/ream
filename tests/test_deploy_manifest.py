"""The two fields that keep a deploy from dropping the polls it is holding.

WHAT THIS CAN AND CANNOT PROVE, said plainly because the distinction matters
here. There is no behaviour to execute: the manifest IS the artefact, and what
it means is decided by the Kubernetes API server, not by this repo. So these
are text assertions, which is the weak kind -- and they were written only
after `kubectl apply --dry-run=server` confirmed the real API keeps both
fields through a round trip (21 Sep 2026, cluster v1.36.0). That was the
verification; this is the ratchet that stops a later tidy-up removing it.

The one thing asserted as a RELATIONSHIP rather than as a number is the one
that can rot silently: a grace period shorter than the drain would have the
kubelet SIGKILL the pod mid-drain, which looks exactly like no drain at all.
"""

import re
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "deploy" / "deployment.yaml"


def _yaml() -> str:
    """The manifest with its comment lines removed.

    Written after the first version of the exec test matched the word "exec:"
    inside the very comment explaining why exec is not used -- a test passing
    or failing on prose about the code rather than the code. The same shape
    cost an hour in the gallery walkthrough on 20 Sep.
    """
    return "\n".join(
        line for line in MANIFEST.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _seconds(pattern: str) -> int:
    found = re.search(pattern, _yaml())
    assert found, f"{pattern!r} is not in deploy/deployment.yaml"
    return int(found.group(1))


def test_the_pod_keeps_serving_while_its_endpoint_is_withdrawn():
    """Operator's waiter, 21 Sep: 3 of 13 long-polls 502'd across a rollout.

    begin_shutdown() was never the gap -- it releases every ALREADY-HELD poll
    the instant SIGTERM lands. The gap is a poll that ARRIVES while the pod is
    terminating, in the moment before the ingress has caught up with the
    withdrawn endpoint. preStop runs before SIGTERM, so this is the window.
    """
    assert _seconds(r"sleep:\n\s+seconds: (\d+)") >= 5, (
        "the drain is too short to cover endpoint propagation; a poll that "
        "arrives during termination goes to a socket that is closing -> 502"
    )


def test_the_drain_is_the_native_sleep_and_not_an_exec():
    """An exec preStop whose binary is missing fails with a warning and the pod
    terminates immediately. The drain then buys nothing while still appearing
    in the manifest -- a broken guard that reads as a working one, which is the
    failure shape this repo keeps meeting. The native action needs no binary.
    """
    body = _yaml()
    assert "preStop:\n              sleep:" in body
    assert "exec:" not in body, "a preStop exec depends on a binary being present"


def test_the_grace_period_outlasts_the_drain():
    """Asserted as a relationship, because two independent numbers drift. A
    grace period at or below the drain means the kubelet SIGKILLs the pod
    part-way through draining, which is indistinguishable from no drain.
    """
    drain = _seconds(r"sleep:\n\s+seconds: (\d+)")
    grace = _seconds(r"terminationGracePeriodSeconds: (\d+)")
    assert grace > drain, f"grace {grace}s does not outlast the {drain}s drain"
    assert grace - drain >= 15, (
        f"only {grace - drain}s left after the drain for the process to stop"
    )


def test_the_drain_is_scaled_to_the_ingress_not_to_the_longest_poll():
    """A drain as long as the longest wait= we accept (300s) would add five
    minutes to every deploy and buy nothing: the holds are released instantly
    by begin_shutdown, they do not need waiting out. Recorded as a test because
    "make the drain longer" is the obvious wrong fix if this 502s again.
    """
    assert _seconds(r"sleep:\n\s+seconds: (\d+)") <= 30
