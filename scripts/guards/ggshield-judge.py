#!/usr/bin/env python3
"""ggshield-judge.py: run a ggshield commit-range scan and pass it only when it provably finished.

    python3 -I ggshield-judge.py [--label TEXT] RANGE -- GGSHIELD [GLOBAL OPTION...]

Called by ggshield-gate.sh, which owns the rest (the skip switch, where ggshield may live, the fixed
settings, the commit that stands for the staged change, and what each verdict means to the commit).

THIS IS A COPY of runcove's cove-ci/scripts/ci/ggshield-judge.py, the house's one judge for its ggshield
scans, taken from runcove commit 41c9506d432a88d53d3388e9e00d73d1956b8641. Keep the two in step: a change
to the rule there is made here too, and this paragraph then names the new commit. The code is the same
as there: this paragraph and the file-level `# ruff: noqa` line after this docstring are the only
differences (the other docstring paragraphs are runcove's, and name runcove's own callers).

Runs `GGSHIELD [GLOBAL OPTION...] secret scan --json commit-range RANGE` in the caller's directory, with
stdin /dev/null and ggshield's output kept in memory (never in a file, never printed), and lists the
commits RANGE names with `git rev-list RANGE --` the same way (RANGE split on whitespace, as ggshield
splits it). GGSHIELD is a path, or a name looked up on PATH. The environment is the caller's, except
that GITGUARDIAN_DONT_LOAD_ENV=1 is set (no .env is loaded) and GITGUARDIAN_EXIT_ZERO,
GITGUARDIAN_FAIL_ON_SERVER_ERROR, GITGUARDIAN_DOTENV_PATH and GG_MAX_DOC_SIZE (which lowers the size
over which ggshield skips a file) are dropped. ggshield runs in a process group
of its own and is given TIMEOUT seconds (600; the environment variable GGSHIELD_JUDGE_TIMEOUT can lower
it): a scan that does not finish in time, or a judge sent SIGINT, SIGTERM or SIGHUP, has the whole group
killed and is incomplete. A judge killed with SIGKILL cannot do that: ggshield, in a session of its own,
runs on until it ends or the job around it is cleaned up (it holds no verdict then: the caller gets none).

Why: ggshield 1.56.1 exits 0 and prints "No secrets have been found" when a scan request fails
(a server error, a refused or rate-limited request, a dropped connection, a reply that is not JSON);
it says so only on stderr. So its exit status alone is never taken as a pass.

The scan PASSES (exit 0, stdout "clean") only when all of these hold:
  * ggshield exited 0, within the time it is given;
  * no line of its stderr, with ANSI colour codes stripped, starts with "Error" or is a debug-log error
    line ("<date> <time> <pid> [E] ..."), and none says "Scanning failed" or "Results may be incomplete";
  * no such line says a file was skipped ("Skipped <file>: <reason>", after a debug-log prefix
    "<date> <time> <pid> [I] " or not): ggshield 1.56.1 leaves a file unscanned when it is over its
    document size limit (1 MiB) or the payload limit, its encoding cannot be detected, or it cannot be
    sought or found, exits 0 and says so only there, so such a scan is incomplete;
  * its stdout is exactly one JSON document, read strictly (UTF-8 with no BOM, no duplicate keys, no NaN,
    nothing before or after it but whitespace), and an object of type commit-range;
  * no "errors" entry anywhere in it is non-empty;
  * its scans list each commit once, and those commits are exactly the ones RANGE names;
  * every incident count is a whole number, and they add up: incidents per file, per commit, in total;
  * it holds no incident: no non-empty "incidents" value (a list, a dict, anything) anywhere in it, no
    "total_incidents" or "total_occurrences" count anywhere in it other than a whole 0 (one above 0, or one the
    judge cannot read as a whole number: a string, a negative, a float, a list, a bool, null), and no file entry
    in an "entities_with_incidents" list without an "incidents" list and a whole count equal to its length.
It is a FINDING (exit 1, stdout "finding") when all of that holds except that ggshield exited 1 and the
JSON holds incidents, every one of them in a file's list; each is listed as commit, file and detector,
never the matched text.
A FINDING IN AN INCOMPLETE SCAN (exit 1, stdout "finding-incomplete") is any other run that shows an
incident: a parsed document with a non-empty "incidents" value, an incident count other than a whole 0 or such
a broken file entry anywhere (whatever the other counts say, list or no list, and whatever ggshield's exit), or
stdout that does not parse but holds an "incidents" key with a non-empty value or a count whose value is anything
but a bare 0, also when ggshield ran out of time after writing it, or a process it started still held its
output open. A secret was seen, so it is never a pass,
and a caller does not run the scan again (a second run that fails to reach the same file could come
back clean). Its incidents are listed as a finding's are, where they could be read.
An UNREAD FINDING (exit 1, stdout "finding-unread") is a run where ggshield exited 1, its found-problems
status, without a finding the judge could read in full: stdout that is not one clean document, counts
that do not add up, an error or a skipped file beside it, or no incident at all. It fails, and a caller
does not run the scan again. A failure of this script after ggshield exited 1 is the same. The JSON is
walked without recursion, so no depth of nesting stops the walk, and a stderr the judge cannot write to
changes no verdict (its lines are lost, its word on stdout and its exit are not).
Anything else is INCOMPLETE (exit 2): the commits were not all scanned, so the scan is not a pass.
stdout is "skipped" when ggshield skipped a file (the same file is skipped on every run, so a caller
does not run it again), and "incomplete" otherwise (an error, a time-out: a second run may pass).
That includes a missing ggshield or git, bad arguments, and a failure of this script.

A caller takes the verdict from the exit status AND the one word on stdout (clean, finding,
finding-incomplete, finding-unread, skipped, incomplete), and treats any other pair
(an interpreter that could not run this file, an empty or cut file) as incomplete.
What it prints on stderr: its reasons and the findings, each line starting with LABEL (default
"ggshield"). Of ggshield's own text it prints only error kinds (an HTTP status, an exception's class
name), never a line of it: on a shared machine the terminal is a transcript.

Callers (the one judge for each of runcove's ggshield scans): cove-ci/scripts/ggshield-scan.sh (cove-pr.yml's
secret scan), cove-ci/scripts/ci/stage.sh's ggshield step, and the office VM pre-push hook
scripts/office-vm/files/prepush-secret-scan.sh. cove-ci/scrub/rewrite-history-scan.py (the history
rewrite's repo and chunk scans, judged against a baseline) imports ANSI, ERROR_LINE and
INCOMPLETE_TEXT from this file for its own reading of ggshield's output: keep those names.
Test: bash cove-ci/scripts/tests/test-ggshield-scan.sh (its "judge" rows).
"""
# ruff: noqa: B904, BLE001, F541, PLW1510, S603, S607, SIM115
import json
import os
import re
import signal
import subprocess
import sys

HEX = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
ERROR_LINE = re.compile(r"^(?:Error|(?:\S+ \S+ \S+ )?\[E\]\s)")
INCOMPLETE_TEXT = ("Scanning failed", "Results may be incomplete")
KIND_CODE = re.compile(r"(?:\bHTTP |status_code=|(?<![\w.:/-]))([45]\d\d)(?=[:\s)]|$)")
KIND_CLASS = re.compile(r"\b([A-Z][A-Za-z]*(?:Error|Exception))\b")
REFUSED_OPTIONS = ("--exit-zero", "--json", "--format", "--output", "-o", "--show-secrets", "--")
DROPPED_ENV = ("GITGUARDIAN_EXIT_ZERO", "GITGUARDIAN_FAIL_ON_SERVER_ERROR", "GITGUARDIAN_DOTENV_PATH", "GG_MAX_DOC_SIZE")
# "Skipped <url>: <reason>" (ggshield/core/scanner_ui/plain_text_scanner_ui.py), any reason, with or without
# a debug-log prefix "<date> <time> <pid> [<level>] " (and a logger's "<name>:<line> " after it, if any). The
# ": <reason>" is not required: a newline in the file's name splits the line, and its first half is still a skip.
SKIPPED_LINE = re.compile(r"^(?:\S+ \S+ \S+ \[[A-Z]\]\s+(?:\S+:\d+\s+)?)?Skipped \S")
# stdout that does not parse but shows an "incidents" key with a non-empty value after it, or an incident count
# whose value is anything but a bare 0 (as in a parsed document): a secret was seen
RAW_INCIDENT = re.compile(r'"incidents"\s*:\s*(?:\[\s*[^\]\s]|\{\s*[^}\s]|[^\[{\s\]}n0f"]|"[^"])'
                          r'|"total_(?:incidents|occurrences)"\s*:\s*(?!0\s*(?:[,}\]]|$))\S')
# the counts that say an incident was seen, whatever the lists beside them hold
COUNT_KEYS = ("total_incidents", "total_occurrences")
TIMEOUT = 600  # seconds ggshield's scan may take; GGSHIELD_JUDGE_TIMEOUT can lower it, never raise it

LABEL = "ggshield"


class Incomplete(Exception):
    pass


class Skipped(Incomplete):
    """ggshield skipped a file: the scan is incomplete, and the same on every run."""


class Unread(Exception):
    """ggshield exited 1 (found problems), and the judge could not read a finding in full: final, never retried."""


EXITED_1 = False  # set once ggshield exits 1: from then on no failure of the judge is a retry


class Stopped(BaseException):
    """The judge was sent SIGTERM or SIGHUP while ggshield ran."""


def stop(signum, _frame):
    raise Stopped(signal.Signals(signum).name)


def scan_timeout():
    v = os.environ.get("GGSHIELD_JUDGE_TIMEOUT", "")
    if not v:
        return TIMEOUT
    if not re.fullmatch(r"[0-9]{1,6}", v) or int(v) < 1:
        raise Incomplete("GGSHIELD_JUDGE_TIMEOUT is not a whole number of seconds")
    return min(int(v), TIMEOUT)


def stop_group(p):
    """Kills ggshield and everything it started (its own process group), then reaps it, and returns the stdout
    it wrote (b"" if none could be read). A process that left the group can still hold the pipes open, so the
    last read is bounded too, and what was read before it is kept."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass
    out = b""
    try:
        return p.communicate(timeout=5)[0] or b""
    except subprocess.TimeoutExpired as e:
        out = e.output or b""  # everything read from stdout so far, across both waits
    except (ValueError, OSError):
        pass
    for f in (p.stdout, p.stderr):
        try:
            f.close()
        except OSError:
            pass
    p.wait()
    return out


def run_scan(argv, env):
    """Runs ggshield in a process group of its own, so that on a timeout or a signal to the judge the whole
    group is killed and nothing it started is left holding the pipes. Returns (exit, stdout, stderr)."""
    limit = scan_timeout()
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, stop)
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env, start_new_session=True)
    except OSError as e:
        raise Incomplete(f"ggshield could not be run ({type(e).__name__}: {argv[0]})")
    try:
        out, err = p.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        out = stop_group(p)
        msg = f"ggshield did not finish within {limit} s, so it was stopped"
        if shows_incident(out):
            say(msg)
            raise TimedOutFinding(f"an incident in what ggshield wrote before it was stopped: a finding, not run again")
        raise Incomplete(msg)
    except BaseException:
        stop_group(p)
        raise
    return p.returncode, out, err


class TimedOutFinding(Exception):
    """ggshield ran out of time after writing JSON that shows an incident."""


def say(msg):
    """One line on stderr. A stderr that cannot be written (full, closed) loses the line, never the verdict."""
    if sys.stderr is None:  # never print(file=None): that writes to stdout, where only the verdict goes
        return
    try:
        print(f"{LABEL}: {msg}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        quiet_stderr()


def quiet_stderr():
    """Points stderr at /dev/null, so neither a later line nor the interpreter's flush at exit can fail."""
    try:
        sys.stderr = open(os.devnull, "w")
    except OSError:
        sys.stderr = None


def whole(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def no_dup_keys(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise Incomplete(f"ggshield's JSON repeats the key {k!r}")
        d[k] = v
    return d


def no_constant(name):
    raise Incomplete(f"ggshield's JSON holds {name}, which is not JSON")


def pairs(node):
    """Every (key, value) of every object in the JSON, at any depth, walked with a stack (no recursion, so no
    nesting is too deep for it)."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            for k, v in n.items():
                yield k, v
                stack.append(v)
        elif isinstance(n, list):
            stack.extend(n)


def count_errors(node):
    """Every non-empty "errors" value, at any depth."""
    return sum(len(v) if isinstance(v, (list, dict)) else 1 for k, v in pairs(node) if k == "errors" and v)


def count_incidents(node):
    """The incidents anywhere in the JSON, whatever its counts say: each non-empty "incidents" value counts its
    length if it is a list or a dict, else 1."""
    return sum(len(v) if isinstance(v, (list, dict)) else 1 for k, v in pairs(node) if k == "incidents" and v)


def counts_incident(node):
    """Whether any incident count anywhere in the JSON is other than a whole 0, whatever the lists beside it hold: a
    count above 0, or one the judge cannot read as a whole number ("1", "1,000", "0x1", -1, 1.0, [1], true, null)."""
    return any(k in COUNT_KEYS and not (whole(v) and v == 0) for k, v in pairs(node))


def entry_ok(e):
    """Whether a file entry is whole: a dict with an "incidents" list and a whole total_incidents equal to its length."""
    return isinstance(e, dict) and isinstance(e.get("incidents"), list) and whole(e.get("total_incidents")) \
        and e["total_incidents"] == len(e["incidents"])


def broken_entry(node):
    """Whether any "entities_with_incidents" anywhere in the JSON is not a list of whole file entries. A file is
    listed there because ggshield found something in it, so a broken entry shows an incident."""
    return any(k == "entities_with_incidents" and (not isinstance(v, list) or not all(entry_ok(e) for e in v))
               for k, v in pairs(node))


def strict_load(raw):
    """ggshield's stdout as one JSON document, or Incomplete with why it is not: not UTF-8, a BOM, a duplicate
    key, NaN or Infinity, nothing, or anything but whitespace before or after the one document."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Incomplete("ggshield's output is not UTF-8")
    if text.startswith("\ufeff"):
        raise Incomplete("ggshield's output starts with a byte order mark")
    try:
        return json.loads(text, object_pairs_hook=no_dup_keys, parse_constant=no_constant)
    except Incomplete:
        raise
    except (ValueError, RecursionError):
        raise Incomplete("ggshield's output is not exactly one JSON document")


def shows_incident(raw):
    """Whether stdout shows an incident: parsed strictly, a non-empty "incidents" value or an incident count above
    0 anywhere; or, if it does not parse, an "incidents" key with a non-empty value or such a count anywhere in its
    text."""
    try:
        d = strict_load(raw)
        return count_incidents(d) > 0 or counts_incident(d) or broken_entry(d)
    except (Incomplete, RecursionError):
        return bool(RAW_INCIDENT.search(raw.decode("utf-8", "replace")))


def error_texts(node):
    for k, v in pairs(node):
        if k == "errors" and isinstance(v, list):
            for e in v:
                if isinstance(e, dict) and isinstance(e.get("description"), str):
                    yield e["description"]


def kinds(lines):
    """The error kinds named in ggshield's text: HTTP statuses and exception class names, nothing else."""
    found = []
    for line in lines:
        for k in KIND_CODE.findall(line) + KIND_CLASS.findall(line):
            if k not in found:
                found.append(k)
    return ", ".join(found[:8]) if found else "none named"


def judge(rng, cmd):
    for a in cmd[1:]:
        if a in REFUSED_OPTIONS or any(a.startswith(o + "=") for o in REFUSED_OPTIONS if o.startswith("--")):
            raise Incomplete(f"the option {a} is not given to ggshield here: the judge sets the output itself")
    parts = rng.split()
    if not parts or any(p.startswith("-") and not p.startswith("^") for p in parts):
        raise Incomplete("the range is empty or holds an option")
    try:
        rl = subprocess.run(["git", "rev-list", *parts, "--"], stdin=subprocess.DEVNULL, capture_output=True)
    except OSError as e:
        raise Incomplete(f"git could not be run to list the range's commits ({type(e).__name__})")
    if rl.returncode != 0:
        raise Incomplete(f"git rev-list could not list the range's commits (exit {rl.returncode})")
    want = rl.stdout.decode("ascii", "replace").split()
    if not want or not all(HEX.match(c) for c in want):
        raise Incomplete("the range names no commits, so there is nothing to compare the scan with")

    env = dict(os.environ)
    env["GITGUARDIAN_DONT_LOAD_ENV"] = "1"
    for k in DROPPED_ENV:
        env.pop(k, None)
    global EXITED_1
    returncode, stdout, stderr = run_scan([*cmd, "secret", "scan", "--json", "commit-range", rng], env)
    EXITED_1 = returncode == 1
    err_lines = [ANSI.sub("", ln).strip() for ln in stderr.decode("utf-8", "replace").splitlines()]
    err_lines = [ln for ln in err_lines if ln]

    problems = []
    if returncode < 0:
        problems.append(f"ggshield was killed by signal {-returncode}")
    elif returncode not in (0, 1):
        problems.append(f"ggshield exited {returncode}")
    bad = [ln for ln in err_lines if ERROR_LINE.match(ln)]
    if bad:
        problems.append(f"ggshield exited {returncode} but wrote {len(bad)} error line(s) on stderr")
    if any(t in ln for ln in err_lines for t in INCOMPLETE_TEXT):
        problems.append("ggshield said the scan failed or its results may be incomplete")
    skipped = sum(1 for ln in err_lines if SKIPPED_LINE.match(ln))
    if skipped:
        problems.append(f"ggshield skipped {skipped} file(s) (over a size limit, an encoding it cannot detect, or a "
                        f"file it cannot read), so they were not scanned")

    doc, incidents = None, 0
    try:
        doc = strict_load(stdout)
    except Incomplete as e:
        problems.append(str(e))
        incidents = 1 if shows_incident(stdout) else 0
    found = []
    if doc is not None:
        found = check_doc(doc, want, problems)
        # every incident anywhere in the document, whatever the counts say, in every branch
        incidents = count_incidents(doc)
        if incidents != len(found):
            problems.append(f"ggshield's JSON holds {incidents} incident(s), {len(found)} of them in a file's list")
        # a count other than a whole 0 anywhere, or a broken file entry, is an incident shown, even with no list
        # holding it: final, never retried
        if not incidents and counts_incident(doc):
            problems.append("ggshield's JSON counts an incident (or a count that is not a whole number) but lists none")
            incidents = 1
        elif not incidents and broken_entry(doc):
            problems.append("ggshield's JSON lists a file with no incidents list or no whole count beside it")
            incidents = 1
    if returncode != 1 and not problems and not incidents:
        say(f"{len(want)} commit(s) of {rng} scanned, no secrets found")
        return 0
    for pr in problems:
        say(pr)
    if problems:
        texts = bad + list(error_texts(doc)) if doc is not None else bad
        say(f"error kinds reported: {kinds(texts)}")
    for f in found:
        say(f"a secret: {f}")
    if returncode == 1 and not problems and found:
        say(f"{len(found)} incident(s) found in the {len(want)} commit(s) of {rng}")
        return 1
    if incidents:
        say(f"{len(found) or 'an'} incident(s) shown in a scan of {rng} that was not complete or not clean "
            f"(ggshield exited {returncode}): a finding, not run again")
        return 3
    if returncode == 1:
        raise Unread(f"ggshield exited 1 (it found problems) but the judge could not read a finding in {rng}: "
                     f"a finding, not run again")
    raise (Skipped if skipped else Incomplete)(f"the {len(want)} commit(s) of {rng} were not all scanned")


def check_doc(doc, want, problems):
    """Checks ggshield's commit-range JSON; appends to problems; returns the incidents as text."""
    found = []
    if not isinstance(doc, dict) or doc.get("type") != "commit-range" or not isinstance(doc.get("scans"), list):
        problems.append("ggshield's JSON is not a commit-range result with a list of scans")
        return found
    n = count_errors(doc)
    if n:
        problems.append(f"ggshield's JSON holds {n} error(s)")
    if not whole(doc.get("total_incidents")):
        problems.append("ggshield's JSON has no whole total_incidents count")
        return found
    seen, total = [], 0
    for s in doc["scans"]:
        if not isinstance(s, dict) or s.get("type") != "commit" or not isinstance(s.get("id"), str) \
                or not HEX.match(s["id"]):
            problems.append("a scan in ggshield's JSON is not a commit with an id")
            return found
        if not whole(s.get("total_incidents")):
            problems.append(f"the scan of commit {s['id']} has no whole total_incidents count")
            return found
        ents = s.get("entities_with_incidents", [])
        if not isinstance(ents, list):
            problems.append(f"the scan of commit {s['id']} has a file list that is not a list")
            return found
        sub = 0
        for ent in ents:
            incs = ent.get("incidents") if isinstance(ent, dict) else None
            if not isinstance(incs, list) or not whole(ent.get("total_incidents")) \
                    or ent["total_incidents"] != len(incs):
                problems.append(f"a file in the scan of commit {s['id']} has incident counts that do not add up")
                return found
            sub += len(incs)
            for inc in incs:
                det = inc.get("type") if isinstance(inc, dict) else None
                fn = ent.get("filename")
                found.append(f"detector {det if isinstance(det, str) else '?'!r}, file "
                             f"{fn if isinstance(fn, str) else '?'!r}, commit {s['id']}")
        if sub != s["total_incidents"]:
            problems.append(f"the scan of commit {s['id']} has incident counts that do not add up")
            return found
        seen.append(s["id"])
        total += s["total_incidents"]
    if total != doc["total_incidents"]:
        problems.append("the commits' incident counts do not add up to the total")
    if len(set(seen)) != len(seen):
        problems.append("ggshield's JSON lists a commit more than once")
    missing, extra = set(want) - set(seen), set(seen) - set(want)
    if missing or extra:
        problems.append(f"the commits scanned are not the range's: {len(want)} asked for, {len(missing)} of them "
                        f"not in the result, {len(extra)} in it that the range does not hold")
    return found


def main(argv):
    global LABEL
    if argv[:1] == ["--label"] and len(argv) >= 2:
        LABEL, argv = argv[1], argv[2:]
    if len(argv) < 3 or argv[1] != "--" or not argv[2]:
        say("usage: ggshield-judge.py [--label TEXT] RANGE -- GGSHIELD [GLOBAL OPTION...]")
        return 2
    return judge(argv[0], argv[2:])


if __name__ == "__main__":
    verdict, rc = "incomplete", 2
    try:
        rc = main(sys.argv[1:])
        verdict, rc = {0: ("clean", 0), 1: ("finding", 1), 3: ("finding-incomplete", 1)}.get(rc, ("incomplete", 2))
    except Unread as e:
        verdict, rc = "finding-unread", 1
        say(str(e))
    except TimedOutFinding as e:
        verdict, rc = "finding-incomplete", 1
        say(str(e))
    except Skipped as e:
        verdict = "skipped"
        say(f"{e}: not a pass, and a re-run skips the same file")
    except Incomplete as e:
        say(f"{e}: not a pass")
    except BaseException as e:  # anything else, KeyboardInterrupt included, is not a pass either
        say(f"the judge failed ({type(e).__name__}): not a pass")
    if EXITED_1 and verdict in ("incomplete", "skipped"):  # ggshield found problems: never a retry
        verdict, rc = "finding-unread", 1
        say("ggshield exited 1 (it found problems): a finding, not run again")
    print(verdict, flush=True)
    sys.exit(rc)
