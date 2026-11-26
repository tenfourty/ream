#!/usr/bin/env python3
"""ggshield-judge.py: run a ggshield commit-range scan and pass it only when it provably finished.

    python3 -I ggshield-judge.py [--label TEXT] RANGE -- GGSHIELD [--config-path FILE] [GLOBAL OPTION...]

Called by ggshield-gate.sh, which owns the rest (the skip switch, where ggshield may live, the fixed
settings, the commit that stands for the staged change, and what each verdict means to the commit).

THIS IS A COPY of runcove's cove-ci/scripts/ci/ggshield-judge.py, the house's one judge for its ggshield
scans, taken from runcove commit b5e1c64c1b92f3cfa0c252410466c9f6e79ce07a. Keep the two in step: a change
to the rule there is made here too, and this paragraph then names the new commit. The code is the same
as there: this paragraph and the file-level `# ruff: noqa` line after this docstring are the only
differences (the other docstring paragraphs are runcove's, and name runcove's own callers).

Runs `GGSHIELD [GLOBAL OPTION...] secret scan --json commit-range RANGE` in the caller's directory, with
stdin /dev/null and ggshield's output kept in memory (never in a file, never printed), and lists the
commits RANGE names with `git rev-list RANGE --` the same way (RANGE split on whitespace, as ggshield
splits it). GGSHIELD is a path, or a name looked up on PATH. The environment is the caller's, except
that GITGUARDIAN_DONT_LOAD_ENV=1 is set (no .env is loaded) and GITGUARDIAN_EXIT_ZERO,
GITGUARDIAN_FAIL_ON_SERVER_ERROR, GITGUARDIAN_DOTENV_PATH, GG_MAX_DOC_SIZE (which lowers the size
over which ggshield skips a file), GG_GIT_CONFIG (a git config of its own for ggshield's git) and
GG_MAX_DOCS_PER_COMMIT (the size of its batches of documents) are dropped. ggshield runs in a process group
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
  * each commit's "entities_with_incidents" list names, one entry per document, exactly the documents ggshield
    sends for that commit (compared by file name, per commit, as a multiset: a name with no entry, or an entry for
    no document sent, fails). ggshield matches the API's replies to the documents it sent with zip(), which
    silently drops the documents a short reply does not answer, so a reply that covers fewer files than were sent
    shows only as fewer entries. The judge predicts the documents with the git calls and the parsing of ggshield
    1.56.1 itself (Commit.from_sha: `git show SHA --raw -z`, then batches of 20 paths with `--raw -z --patch --`, a
    rename's old path too, the diffs split as parse_patch does, less the default ignored paths and the config's
    ignored_paths), in the same repository, so a merge's combined diff, diff.renames and a submodule's
    trailing-slash path come out as ggshield's own git gives them;
  * no git attribute (diff, -diff, binary, a diff driver) in effect hides a changed file from ggshield's diff: the
    prediction is made a second time with attributes off, and a file only that one shows fails the scan. (The
    wrappers export GIT_ATTR_SOURCE=<the empty tree> and GIT_ATTR_NOSYSTEM=1, which the judge reads as attributes
    already off, and skips the second prediction. GIT_ATTR_SOURCE is git 2.40 and later: an older git ignores it, so
    the checkout's .gitattributes still applies there, and neither check sees .git/info/attributes.)
  * the ggshield config is one the judge can read, and it is the one ggshield reads. Two ways to name it:
      - `--config-path FILE` among the global options: ggshield reads that file alone (a missing file is the empty
        config), and the judge reads the same file;
      - the office VM wrapper, which the callers mark by setting COVE_HOOKS_GGSHIELD_BASE_CONFIG (empty or not). The
        wrapper filters that source file and gives ggshield its own file, the empty config when its filter rejects
        the source, so the judge does not read the source: it names a private directory in
        COVE_HOOKS_GGSHIELD_EXPORT_DIR and the wrapper writes there the config it gave ggshield ("config") and the
        GIT_* variables ggshield's git runs under ("env": the wrapper's pins, diff.renames=false among them). The
        judge reads the first for the ignored paths and predicts documents under the second. A wrapper that hands
        back nothing (an older one) leaves the scan incomplete.
    With neither, ggshield merges a config of the home directory with the repository's, which the judge cannot
    read to tell which paths ggshield skips, so it REFUSES: the scan is incomplete. It also refuses a file that is
    not version 2, holds a key outside version, verbose, secret (show_secrets, ignored_paths, ignored_matches,
    ignored_detectors, ignore_known_secrets) and max_commits_for_hook, a key twice, a flow list, a quoting or
    pattern it cannot read as ggshield does, and anything YAML would read differently from the line-by-line reading
    the judge makes: a byte order mark, a control character, NEL, LS or PS (YAML ends a line at them), a quoted
    string that does not close on its line, a flow collection other than [], a block scalar, an anchor, alias or
    tag. An empty file is the empty config;
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
# ruff: noqa: B904, BLE001, F541, FURB167, PIE810, PLW1510, S603, S607, SIM115
import collections
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile

HEX = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
ERROR_LINE = re.compile(r"^(?:Error|(?:\S+ \S+ \S+ )?\[E\]\s)")
INCOMPLETE_TEXT = ("Scanning failed", "Results may be incomplete")
KIND_CODE = re.compile(r"(?:\bHTTP |status_code=|(?<![\w.:/-]))([45]\d\d)(?=[:\s)]|$)")
KIND_CLASS = re.compile(r"\b([A-Z][A-Za-z]*(?:Error|Exception))\b")
REFUSED_OPTIONS = ("--exit-zero", "--json", "--format", "--output", "-o", "--show-secrets", "--")
DROPPED_ENV = ("GITGUARDIAN_EXIT_ZERO", "GITGUARDIAN_FAIL_ON_SERVER_ERROR", "GITGUARDIAN_DOTENV_PATH", "GG_MAX_DOC_SIZE",
               "GG_GIT_CONFIG", "GG_MAX_DOCS_PER_COMMIT", "COVE_HOOKS_GGSHIELD_EXPORT_DIR")
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
    # Under the office VM wrapper (COVE_HOOKS_GGSHIELD_BASE_CONFIG is set, empty or not) the wrapper hands back, in a
    # directory of the judge's own, the config it gave ggshield and the GIT_* variables ggshield's git ran under
    export, export_err, xd = None, None, None
    if WRAPPER_VAR in os.environ:
        xd = tempfile.mkdtemp(prefix="ggshield-judge-")
        env["COVE_HOOKS_GGSHIELD_EXPORT_DIR"] = xd
    try:
        returncode, stdout, stderr = run_scan([*cmd, "secret", "scan", "--json", "commit-range", rng], env)
        if xd:
            try:
                export = read_export(xd)
            except Incomplete as e:
                export_err = e
    finally:
        if xd:
            shutil.rmtree(xd, ignore_errors=True)
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
        try:
            if export_err is not None:
                raise export_err
            predictor = Predictor(env, ignored_regexes(cmd, export), export[1] if export else None)
        except Incomplete as e:  # no config the judge can read: no prediction, and the scan is not a pass
            problems.append(str(e))
            predictor = None
        found = check_doc(doc, want, problems, predictor)
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


# ggshield 1.56.1 sends no document for a path matching one of these or one of the config's ignored_paths
# (ggshield/cmd/secret/scan/secret_scan_common_options.py:31, ggshield/core/filter.py: translate_user_pattern)
DEFAULT_IGNORED = ["**/.git/*/**/*", "**/.pytest_cache/**/*", "**/.mypy_cache/**/*", "**/.venv/**/*", "**/.eggs/**/*",
                   "**/.eggs-info/**/*", "**/vendor/**/*", "**/vendors/**/*", "**/node_modules/**/*", "top-1000.txt*",
                   "**/*.storyboard*", "**/*.xib", "**/*.mdx*", "**/*.sops"]
BAD_PATTERN = re.compile(r"(\*\*\*)|(\*\*[^/])|([^/]\*\*)")
# The config the judge reads, as ggshield 1.56.1 reads a version 2 file (ggshield/core/config/user_config.py): the
# keys it understands, and no others (a key it does not know is a config it may read differently, so it is refused)
CONFIG_TOP_KEYS = ("version", "secret", "max_commits_for_hook", "verbose")
CONFIG_SECRET_KEYS = ("show_secrets", "ignored_paths", "ignored_matches", "ignored_detectors", "ignore_known_secrets")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
WRAPPER_VAR = "COVE_HOOKS_GGSHIELD_BASE_CONFIG"  # set (empty or not) only for the office VM wrapper
GIT_TIMEOUT = 120  # seconds for one git call that mirrors one of ggshield's
DOCS_PER_SHOW = 20  # ggshield/core/scan/commit.py: _MAX_DOCS_PER_COMMIT (GG_MAX_DOCS_PER_COMMIT is dropped from the env)


def ignore_regex(pat):
    if not pat or BAD_PATTERN.search(pat):
        raise Incomplete(f"the ignored path {pat!r} is not a pattern ggshield accepts")
    pat = "".join(f"\\{c}" if c in ".^$+*?{}()[]\\|" else c for c in pat)
    if pat[-1] != "/":
        pat += "$"
    pat = "^" + pat[1:] if pat[0] == "/" else "(^|/)" + pat
    pat = re.sub(r"\\\*\\\*/", "([^/]+/)*", pat)
    return re.compile(re.sub(r"\\\*", "([^/]+)", pat))


def unreadable_value(body, no):
    """Refuses a line, which the judge skips, that holds a value YAML could read across lines or as a collection: a
    quoted string that does not close on the line, a flow list or mapping (other than empty), a block scalar, an
    anchor, alias or tag. Every value is looked at: after `- ` (any number) and after `key: `, where a key is a plain
    word (`[A-Za-z_][A-Za-z0-9_]*`) and nothing else. What stands before a `: ` and is not a plain word (`? 'x`,
    `&a 'x`) is not skipped as a key but read as the value, and a plain value that holds `: ` (or ends in `:`) is
    refused, as YAML would read it as a key. A plain value must start with a letter, a digit, `_`, `.`, `/` or `~`:
    whatever else could start one is a YAML indicator (`? `, `: `, `&`, `*`, `!`, `|`, `>`, `%`, `@`, a backtick) or
    is refused for not being one of these, so `? 'x` is refused as a value and never read as a key marker."""
    rest = body
    while True:  # only a plain word is a key: what else comes before a `: ` is checked whole below
        m = re.match(r"(?:-[ \t]+|[A-Za-z_][A-Za-z0-9_]*:[ \t]+)(.*)$", rest)
        if not m:
            break
        rest = m.group(1)
    v = rest.strip(" \t")
    if not v or v[0] == "#":
        return
    if v[0] == "'":
        ok = re.fullmatch(r"'(?:[^']|'')*'[ \t]*(?:#.*)?", v)
    elif v[0] == '"':
        ok = re.fullmatch(r'"(?:[^"\\]|\\.)*"[ \t]*(?:#.*)?', v)
    elif v[0] in "[{":
        ok = re.fullmatch(r"(?:\[\]|\{\})[ \t]*(?:#.*)?", v)
    else:
        plain = re.sub(r"[ \t]+#.*$", "", v)  # a `: ` in a comment is no key
        ok = re.match(r"[A-Za-z0-9_./~]", v) and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*:", plain) or not re.search(r":(?:[ \t]|$)", plain))
    if not ok:
        no("a line it does not read holds a quoted string that is not closed on the line, a flow collection, a block "
           "scalar, an anchor, alias or tag, a plain value that starts with anything but a letter, digit, `_`, `.`, `/` or "
           "`~`, or a plain value with a `: ` in it whose key is not a plain word")


def config_ignored(path):
    """The ignored_paths of a ggshield config file, which must be a version 2 file of keys the judge understands, read
    as plain lines (a block mapping, `ignored_paths` a block list of plain or simply quoted strings). Anything else
    is Incomplete: the judge cannot then tell which files ggshield skips. An empty file (or one of comments) is the
    empty config."""
    def no(why):
        raise Incomplete(f"the ggshield config {path} is refused: {why}")
    try:
        with open(path, "rb") as f:
            raw = f.read((1 << 20) + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        no(f"it could not be read ({type(e).__name__})")
    if len(raw) > 1 << 20 or re.search(r"[\x00-\x09\x0b-\x1f\x7f\x85\u2028\u2029\ufeff]", text.replace("\r\n", "\n")):
        no("it is over 1 MiB, or holds a byte order mark, a tab or another control character, or a line break other than LF and CR LF "
           "(YAML ends a line at NEL, LS and PS too, where the lines the judge reads do not)")
    lines = [ln for ln in re.split(r"\r?\n", text) if ln.strip(" \t") and not ln.lstrip(" ").startswith("#")]
    if not lines:
        return []
    top, seen_top, seen_sec, cur, child, out = None, set(), set(), None, None, []
    list_indent, need_items = None, False
    for ln in lines:
        indent = len(ln) - len(ln.lstrip(" "))
        body = ln[indent:]
        if body.startswith("\t") or body.startswith(("---", "...", "%", "&", "*", "!")):
            no("a tab, a document marker or an anchor, alias or tag")
        if indent == 0:
            m = re.match(r"([a-z_]+):(?:[ \t]+(.*?))?[ \t]*$", body)
            if not m or m.group(1) not in CONFIG_TOP_KEYS or m.group(1) in seen_top:
                no("a top-level line that is not a key it understands (version, secret, max_commits_for_hook), or a key twice")
            top, val = m.group(1), re.sub(r"[ \t]+#.*$", "", m.group(2) or "").strip(" \t")
            seen_top.add(top)
            if top == "version" and val != "2":
                no("it is not version 2")
            if top == "secret" and val:
                no("secret is not a block mapping")
            if top == "max_commits_for_hook" and not re.fullmatch(r"[0-9]{1,9}", val):
                no("max_commits_for_hook is not a plain number")
            if top == "verbose" and val not in ("true", "false"):
                no("verbose is not true or false")
            cur, child, list_indent = None, None, None
            continue
        if top != "secret":
            no(f"a nested line under {top}")
        if child is None:
            child = indent
        if indent == child and not body.startswith("-"):
            m = re.match(r"([a-z_]+):(?:[ \t]+(.*?))?[ \t]*$", body)
            if not m or m.group(1) not in CONFIG_SECRET_KEYS or m.group(1) in seen_sec:
                no("a line under secret that is not a key it understands, or a key twice")
            cur, val, list_indent = m.group(1), re.sub(r"[ \t]+#.*$", "", m.group(2) or "").strip(" \t"), None
            seen_sec.add(cur)
            if cur != "ignored_paths":
                unreadable_value(body, no)
            if cur == "ignored_paths" and val and val != "[]":
                no("ignored_paths is not a block list")
            need_items = cur == "ignored_paths" and not val
            continue
        if indent < child or cur is None:
            no("a line indented out of place")
        if cur != "ignored_paths":
            unreadable_value(body, no)  # the judge does not read this line: YAML must not read it as more than one
            continue
        if not body.startswith("- ") or (list_indent is not None and indent != list_indent):
            no("ignored_paths holds a line that is not one plain list item")
        list_indent, need_items = indent, False
        it = body[2:].strip(" \t")
        if re.match(r"[-?](?: |$)", it):
            no("an ignored path starts with `- ` or `? `, which YAML reads as a nested list or a complex key")
        if it[:1] in "\"'":
            q = it[0]
            end = it.find(q, 1)
            rest = it[end + 1:].strip(" \t") if end > 0 else "?"
            if end < 1 or (rest and not rest.startswith("#")) or "\\" in it[1:end] or (q == "'" and "''" in it):
                no("an ignored path is a quoted string the judge cannot read as is")
            it = it[1:end]
        else:
            it = re.sub(r"[ \t]+#.*$", "", it).strip(" \t")
            if not it or it[0] in "[{|>@`%,&*!#" or ": " in it or it.endswith(":"):
                no("an ignored path is not a plain string")
        out.append(it)
    if "version" not in seen_top:
        no("it has no version: 2 line")
    if need_items:
        no("ignored_paths holds no list item")
    return out


def named_config(cmd):
    """The config file ggshield reads when the caller names it with --config-path (ggshield then reads that file
    alone), else None: ggshield then merges a config of the home directory with the repository's, which the judge
    cannot read."""
    cfg = None
    for k, a in enumerate(cmd):
        if a == "--config-path" and k + 1 < len(cmd):
            cfg = cmd[k + 1]
        elif a.startswith("--config-path="):
            cfg = a.split("=", 1)[1]
    return cfg


def read_export(d):
    """(ignored paths of the config, GIT_* environment) that the office VM wrapper wrote in directory d for this run:
    the config ggshield was handed (after the wrapper's own filter and its fallbacks to the empty config) and the
    variables its git runs under. Incomplete when either is missing (an older wrapper, or one that wrote nothing)."""
    try:
        with open(d + "/env", "rb") as f:
            raw = f.read((1 << 20) + 1)
        if len(raw) > 1 << 20:
            raise ValueError("too large")
        pins = {}
        for item in raw.decode("utf-8").split("\0"):
            if item:
                k, sep, v = item.partition("=")
                if not sep or not k.startswith("GIT_"):
                    raise ValueError("odd entry")
                pins[k] = v
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise Incomplete(f"the ggshield wrapper did not hand over the git settings its ggshield ran under "
                         f"({type(e).__name__}), so the judge cannot tell which documents it sent; run `setup-office.sh up` to rewrite the wrapper")
    try:
        return config_ignored(d + "/config"), pins
    except Incomplete as e:
        raise Incomplete(str(e).replace(d + "/config", "the config the wrapper gave ggshield"))


def ignored_regexes(cmd, export=None):
    """The patterns ggshield skips paths by: its defaults and the ignored paths of the config it reads: the wrapper's
    (`export`, what read_export gave) or else the one the caller names."""
    if export is not None:
        ignored = export[0]
    else:
        cfg = named_config(cmd)
        if cfg is None:
            raise Incomplete("no config file is named (--config-path), so ggshield would merge a config of the home "
                             "directory with the repository's, which the judge cannot read to tell which files "
                             "ggshield skips")
        ignored = config_ignored(cfg)
    return [ignore_regex(p) for p in DEFAULT_IGNORED + ignored]


# ggshield's documents for a commit, predicted with the git calls and the parsing ggshield 1.56.1 itself makes
# (ggshield/core/scan/commit.py: Commit.from_sha; commit_utils.py: parse_patch, PatchFileInfo; commit_information.py;
# utils/git_shell.py: git; utils/files.py: is_path_excluded). A merge commit's combined diff, a .gitattributes
# diff setting, a submodule's trailing-slash rule and diff.renames are all then what ggshield's git said.
RAW_FILE_SEP = re.compile(r"[\n\0]:")
OLD_NAME_RX = re.compile(r"^--- a/(.*?)\t?$", re.M)
NEW_NAME_RX = re.compile(r"^\+\+\+ b/(.*?)\t?$", re.M)
AUTHOR_RX = re.compile(r"Author:\s(?P<author>.*?) <(?P<email>.*?)>\nDate:\s+(?P<date>.+)")


def git_env(scan_env, attrs_off=False):
    """ggshield's own environment for git: LANG=C and no global or system config, as in utils/git_shell.py: git."""
    env = dict(scan_env, LANG="C", GIT_CONFIG_GLOBAL="", GIT_CONFIG_SYSTEM="")
    if attrs_off:
        env.update(GIT_ATTR_SOURCE=EMPTY_TREE, GIT_ATTR_NOSYSTEM="1")
    return env


def gg_git(args, env):
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", "-c", "safe.directory=*", *args],
                           stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise Incomplete(f"git could not be run to predict ggshield's documents ({type(e).__name__})")
    if r.returncode != 0:
        raise Incomplete(f"git {args[0]} failed (exit {r.returncode}) in predicting ggshield's documents")
    return r.stdout.decode("utf-8", errors="ignore").rstrip()


def raw_files(header):
    """(info, [(old path or None, path)]) of a `git show --raw -z` header, as PatchHeader.from_string and
    PatchFileInfo.from_string read it, with the same refusal of a status letter they do not know."""
    info, *lines = RAW_FILE_SEP.split(header)
    files = []
    for line in lines:
        try:
            prefix, path, *rest = line.rstrip("\0").split("\0")
        except ValueError:
            raise Incomplete("a line of git's raw header is in a form ggshield cannot read")
        status = prefix.rsplit(" ", 1)[-1].rstrip("0123456789")
        if not any(c in status for c in "MUCATRD"):
            raise Incomplete("a line of git's raw header has a status ggshield cannot read")
        files.append((pathlib.Path(path) if rest else None, pathlib.Path(rest[0] if rest else path)))
    return info, files


def excluded(path, skip):
    """is_path_excluded: a path that is a directory in the current directory is tried with a trailing slash."""
    s = str(pathlib.PurePosixPath(path))
    if path.is_dir():
        s += "/"
    return any(rx.search(s) for rx in skip)


def patch_docs(patch, skip):
    """The paths parse_patch yields a document for."""
    tokens = patch.split("\0diff ", 1)
    if len(tokens) == 1:
        return []
    _, files = raw_files(tokens[0])
    out = []
    for d in re.split(r"^diff ", tokens[1], flags=re.M):
        try:
            head = d[:d.index("\n@@") + 1]
        except ValueError:
            continue
        m = NEW_NAME_RX.search(head) or OLD_NAME_RX.search(head)
        if not m:
            raise Incomplete("a diff of git's patch names no path, so ggshield cannot read it either")
        path = pathlib.Path(m.group(1))
        if excluded(path, skip):
            continue
        if not any(p == path for _, p in files):
            raise Incomplete("a diff of git's patch names a path its raw header does not list")
        out.append(str(path))
    return out


def predicted_docs(sha, skip, env):
    """The paths ggshield 1.56.1 sends a document for in commit SHA: `git show SHA --raw -z` for the paths, then
    `git show SHA --raw -z --patch -- <paths>` in batches of 20 (a rename's old path too), each patch split into
    its diffs, less the ignored paths."""
    info, files = raw_files(gg_git(["show", sha, "--raw", "-z"], env))
    if not AUTHOR_RX.search(info):
        raise Incomplete(f"git show of commit {sha} has no author and date ggshield can read")
    renames = {p: old for old, p in files if old is not None}
    paths = [p for _, p in files]
    docs = []
    for i in range(0, len(paths), DOCS_PER_SHOW):
        cmd = ["show", sha, "--raw", "-z", "--patch", "--"]
        for p in paths[i:i + DOCS_PER_SHOW]:
            if p in renames:
                cmd.append(str(renames[p]))
            cmd.append(str(p))
        docs += patch_docs(gg_git(cmd, env), skip)
    return docs


class Predictor:
    """What ggshield sends for each commit: `docs(sha)` is the multiset of document paths, `hidden(sha)` the multiset
    of paths that would have a document but for a git attribute (diff, -diff, binary, a diff driver) in effect here."""

    def __init__(self, scan_env, skip, pins=None):
        if pins is not None:  # the git settings the wrapper ran ggshield under replace the judge's own GIT_* ones
            scan_env = {**{k: v for k, v in scan_env.items() if not k.startswith("GIT_")}, **pins}
        self.env, self.skip = git_env(scan_env), skip
        self.attrs_already_off = scan_env.get("GIT_ATTR_SOURCE") == EMPTY_TREE and scan_env.get("GIT_ATTR_NOSYSTEM") == "1"

    def docs(self, sha):
        return collections.Counter(predicted_docs(sha, self.skip, self.env))

    def hidden(self, sha):
        if self.attrs_already_off:
            return collections.Counter()
        off = collections.Counter(predicted_docs(sha, self.skip, git_env(self.env, attrs_off=True)))
        return off - self.docs(sha)


def check_entries(sha, ents, predictor, problems):
    """Appends a problem when the entries of a commit's scan are not the documents ggshield sends for it, by name,
    and a separate one when a git attribute hides a changed file from ggshield's diff."""
    try:
        want = predictor.docs(sha)
        hidden = predictor.hidden(sha)
    except Incomplete as e:
        problems.append(str(e))
        return
    got = collections.Counter(e.get("filename") if isinstance(e, dict) else None for e in ents)
    short, extra = want - got, got - want
    if short or extra:
        names = ", ".join(repr(n)[:80] for n in sorted(short)[:3])
        problems.append(f"the scan of commit {sha} has {sum(got.values())} file result(s) for the {sum(want.values())} "
                        f"document(s) ggshield sends: {sum(short.values())} sent without a result"
                        f"{' (' + names + ')' if names else ''}, {sum(extra.values())} result(s) not for a document "
                        f"sent; the reply may cover fewer files than were sent")
    if hidden:
        problems.append(f"a git attribute (diff, -diff, binary or a diff driver) hides {sum(hidden.values())} changed "
                        f"file(s) of commit {sha} from ggshield's scan, so their text is not scanned")


def check_doc(doc, want, problems, predictor):
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
        # ggshield writes one entry, named for its path, for each document it sent for a commit (clean files too) and no
        # list at all for a commit it sent none for. A reply with fewer results than documents leaves the last ones
        # unanswered (zip), so each commit's entries are compared by name with the documents predicted for it
        ents = s.get("entities_with_incidents", [])
        if not isinstance(ents, list):
            problems.append(f"the scan of commit {s['id']} has a file list that is not a list")
            return found
        if predictor is not None and s["id"] in want:
            check_entries(s["id"], ents, predictor, problems)
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
