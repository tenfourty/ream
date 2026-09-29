"""Mark up what changed between two versions of an HTML page.

Pure and dependency-free. ``render_changes`` returns the NEW page with inserted
text in ``<ins class="ar-diff-ins">``, deleted text in ``<del class="ar-diff-del">``
at the place it was removed, and a small chip where an element with no text
disappeared. Tags are never wrapped and deleted tags are dropped, so the tree is
always the new document's own.
"""

import re
from collections import Counter
from difflib import SequenceMatcher

MAX_TOKENS = 60_000
MAX_SCRIPT_SHARE = 0.7
# difflib's cost on the differing middle is about the sum, over old tokens, of how
# often that token occurs in the new middle. Past this the diff could take
# seconds (a page of one repeated phrase), so the page falls back instead.
MAX_MATCH_COST = 4_000_000

REASON_LARGE = "too large to mark up changes"
REASON_SCRIPT = "this page draws itself with script"

_TOKEN = re.compile(
    r"""
    <!--.*?-->
  | <script\b(?:"[^"]*"|'[^']*'|[^>"'])*>.*?</script\s*>
  | <style\b(?:"[^"]*"|'[^']*'|[^>"'])*>.*?</style\s*>
  | <[a-zA-Z/!?](?:"[^"]*"|'[^']*'|[^>"'])*>
  | \s+
  | [^\s<]+
  | <
    """,
    re.DOTALL | re.IGNORECASE | re.VERBOSE,
)
_BODY = re.compile(r"""<body\b(?:"[^"]*"|'[^']*'|[^>"'])*>""", re.IGNORECASE)
_OPEN_TAG = re.compile(r"<[a-zA-Z]")

_INS = '<ins class="ar-diff-ins">'
_DEL = '<del class="ar-diff-del">'
_CHIP = '<span class="ar-diff-chip">removed</span>'

_WS, _WORD, _TAG = 0, 1, 2


def _kind(tok: str) -> int:
    if tok[0] == "<" and len(tok) > 1:
        return _TAG
    return _WS if tok.isspace() else _WORD


def _is_script(tok: str) -> bool:
    return tok[:7].lower() == "<script"


def _is_opaque(tok: str) -> bool:
    return tok.startswith("<!--") or tok[:6].lower() == "<style" or _is_script(tok)


def _fallback(new_html: str, reason: str) -> tuple[str, str]:
    banner = (
        f'<div class="ar-diff-banner">Showing the page as it now is: {reason}</div>'
    )
    m = _BODY.search(new_html)
    if m:
        return new_html[: m.end()] + banner + new_html[m.end() :], reason
    return banner + new_html, reason


def render_changes(old_html: str, new_html: str) -> tuple[str, str | None]:
    """Return ``(document, fallback_reason)``: the new page with changes marked."""
    old_toks = _TOKEN.findall(old_html)
    new_toks = _TOKEN.findall(new_html)
    if len(old_toks) > MAX_TOKENS or len(new_toks) > MAX_TOKENS:
        return _fallback(new_html, REASON_LARGE)
    script_bytes = sum(len(t.encode()) for t in new_toks if _is_script(t))
    if script_bytes and script_bytes > MAX_SCRIPT_SHARE * len(new_html.encode()):
        return _fallback(new_html, REASON_SCRIPT)

    # Diff over non-whitespace tokens only, so whitespace changes never mark.
    a = [(t, _kind(t)) for t in old_toks]
    a_idx = [i for i, (_, k) in enumerate(a) if k != _WS]
    b_idx = [i for i, t in enumerate(new_toks) if _kind(t) != _WS]
    a_seq = [old_toks[i] for i in a_idx]
    b_seq = [new_toks[i] for i in b_idx]

    # Trim the common ends: keeps a one-word edit on a huge page linear.
    lo = 0
    n = min(len(a_seq), len(b_seq))
    while lo < n and a_seq[lo] == b_seq[lo]:
        lo += 1
    hi = 0
    while hi < n - lo and a_seq[-1 - hi] == b_seq[-1 - hi]:
        hi += 1
    a_mid = a_seq[lo : len(a_seq) - hi]
    b_mid = b_seq[lo : len(b_seq) - hi]

    if a_mid and b_mid:
        freq = Counter(b_mid)
        if sum(freq[t] for t in a_mid) > MAX_MATCH_COST:
            return _fallback(new_html, REASON_LARGE)

    inserted = [False] * len(b_seq)
    deleted_at: dict[int, list[str]] = {}
    if a_mid or b_mid:
        sm = SequenceMatcher(None, a_mid, b_mid, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            for j in range(lo + j1, lo + j2):
                inserted[j] = True
            if i2 > i1:
                b_all_text = (
                    all(_kind(t) != _TAG for t in b_mid[j1:j2])
                    if op == "replace"
                    else True
                )
                deleted_at.setdefault(lo + j1, []).append(
                    _render_deleted(
                        a_seq[lo + i1 : lo + i2],
                        a_idx[lo + i1 : lo + i2],
                        old_toks,
                        b_all_text,
                    )
                )

    ins_at = {b_idx[j] for j, flag in enumerate(inserted) if flag}
    del_before = {
        b_idx[j] if j < len(b_idx) else len(new_toks): "".join(v)
        for j, v in deleted_at.items()
    }

    out: list[str] = []
    pending_ws: list[str] = []
    ins_open = False

    def close_ins():
        nonlocal ins_open
        if ins_open:
            out.append("</ins>")
            ins_open = False

    def flush_ws():
        out.extend(pending_ws)
        pending_ws.clear()

    for i, tok in enumerate(new_toks):
        kind = _kind(tok)
        if kind == _WS:
            pending_ws.append(tok)
            continue
        if i in del_before:
            close_ins()
            flush_ws()
            out.append(del_before[i])
        if kind == _WORD and i in ins_at:
            if ins_open:
                flush_ws()
            else:
                flush_ws()
                out.append(_INS)
                ins_open = True
            out.append(tok)
        else:
            close_ins()
            flush_ws()
            out.append(tok)
    close_ins()
    flush_ws()
    end = len(new_toks)
    if end in del_before:
        out.append(del_before[end])
    return "".join(out), None


def _render_deleted(
    toks: list[str], idxs: list[int], old_toks: list[str], b_all_text: bool
) -> str:
    """Deleted text runs in one ``<del>`` each; a chip if only tags went."""
    parts: list[str] = []
    run: list[str] = []
    has_text = False

    def end_run():
        if run:
            parts.append(_DEL + "".join(run) + "</del>")
            run.clear()

    for tok, i in zip(toks, idxs, strict=True):
        if _kind(tok) == _TAG:
            end_run()
            continue
        has_text = True
        if run and i > 0 and _kind(old_toks[i - 1]) == _WS:
            run.append(" ")
        run.append(tok)
    end_run()
    if (
        not has_text
        and b_all_text
        and any(_OPEN_TAG.match(t) and not _is_opaque(t) for t in toks)
    ):
        return _CHIP
    return "".join(parts)
