import random
import re
import sys
import time

import pytest

from artifacts import scan

FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----"


def test_clean_body_has_no_findings():
    assert scan.scan("<!doctype html><title>Board</title><p>42 issues open</p>") == []


def test_aws_key_shape_is_found_with_its_line():
    findings = scan.scan(f"line one\nconst k = '{FAKE_AWS}'\n")
    assert findings and findings[0].line == 2


def test_private_key_header_is_found():
    assert scan.scan(FAKE_PRIVATE_KEY) != []


def test_shapes_file_still_carries_the_shapes_we_rely_on():
    names = {name for name, _ in scan.SHAPES}
    assert {"aws_access_key_id", "private_key_header"} <= names


def test_a_finding_never_carries_the_matched_text():
    # Finding is a frozen dataclass of exactly (shape, line) -- there is no
    # field a caller could accidentally log or return that holds the secret
    # itself. This is a structural guarantee, checked here so it stays true.
    field_names = set(scan.Finding.__dataclass_fields__.keys())
    assert field_names == {"shape", "line"}
    findings = scan.scan(f"const k = '{FAKE_AWS}'")
    assert findings
    for finding in findings:
        assert FAKE_AWS not in repr(finding)


# --- fail-closed: a malformed shapes source never loads with reduced or
# empty coverage; it refuses to load at all. ---------------------------------


def test_a_shapes_line_missing_its_tab_fails_closed_instead_of_matching_everything():
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes("this_line_has_no_tab_or_pattern")


def test_a_shapes_line_with_an_uncompilable_pattern_fails_closed():
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes("bad_shape\t(unbalanced[")


def test_an_empty_shapes_source_fails_closed_instead_of_scanning_nothing():
    # Zero shapes is indistinguishable, from the outside, from "nothing here
    # ever matches" -- exactly what a caller relying on scan() to gate a
    # publish must never see silently.
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes("# just a comment\n\n")


def test_a_pattern_matching_the_empty_string_is_rejected():
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes("wide\t.*")


def test_duplicate_shape_names_are_rejected():
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes("dup\tfoo\ndup\tbar\n")


def test_a_shapes_pattern_using_a_whole_body_anchor_is_rejected():
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes("bad_anchor\t\\Asecret=")


def test_a_well_formed_shapes_source_parses_without_raising():
    shapes = scan.parse_shapes("# comment\n\nsecret_assignment\tsecret=\n")
    assert [name for name, _ in shapes] == ["secret_assignment"]


@pytest.mark.parametrize(
    "pattern",
    [
        "(?i:[a-z]+_token=)",  # a class
        "(?i:token|secret)=",  # an alternation
        "(?i:pass(word)?=)",  # a group and a quantifier
        "(?i:api_?key)",  # a quantifier
        "(?i:token.)",  # any character
        "(?i:\\S+=)",  # an escape that lowercases to its opposite (\S -> \s)
        "(?i:\\x41KIA)",  # an escape that names an uppercase letter
    ],
)
def test_a_case_insensitive_group_that_is_not_a_plain_literal_is_rejected(pattern):
    # scan() searches a (?i:...) shape case-sensitively, with the group's text
    # lowercased, over an ASCII-lowercased copy of the view. That is only
    # the same search when the group is plain literal text: lowercasing
    # `\S` gives `\s`, and `\x41` never matches a lowercased body. Such a
    # shape would load and then quietly match the wrong thing.
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes(f"unsafe_fold\t{pattern}\n")


@pytest.mark.parametrize(
    "pattern",
    ["(?i:X-Api-Key:)", "(?i:client_secret=)", "(?i:api\\.key=)", "(?i:bearer)\\s+[A-Za-z0-9]{16,}"],
)
def test_a_case_insensitive_group_of_plain_literal_text_still_loads(pattern):
    assert [name for name, _ in scan.parse_shapes(f"safe_fold\t{pattern}\n")] == ["safe_fold"]


@pytest.mark.parametrize(
    "pattern",
    [
        "(?i:api_key=)AKIA",  # an uppercase literal outside the group
        "(?i:token=)[A-Z0-9]{20}",  # an uppercase-only class outside the group
        "(?i:token=)\\x41",  # an escape that names an uppercase letter
        "(?i:token=)[^A-Z]",  # negated, naming A-Z without a-z
        "(?i:token=)[A-Za-f]",  # a counterpart, but only for A-F: G-Z would never match
        "(?i:token=)[^a-z]+",  # negated, a-z without A-Z: `A` matches, its lowercase does not
        "(?i:token=)?\\d{6}",  # a quantifier after a group (bare-text folding broke it)
    ],
)
def test_a_shape_the_lowercased_search_would_narrow_is_rejected(pattern):
    # A (?i:...) shape is searched, in full, over a lowercased copy of the
    # body, with each group folded to its lowercased text. Anything that
    # search would stop matching -- an uppercase letter (the copy has none),
    # or a quantifier after a group -- is refused at load.
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes(f"narrowed\t{pattern}\n")


@pytest.mark.parametrize(
    "pattern",
    [
        "(?i:token=)\\S+",  # case-neutral escapes are not letters
        "(?i:token=)[A-Za-z0-9]{20}",  # a class admitting both cases
        "(?i:token=)[^A-Za-z]",  # negated too
        "(?i:key=)[0-9A-Fa-f]{32}",  # hex: each uppercase letter has its counterpart
        "(?i:token=)[A-Fa-z]",  # the counterpart present, and wider
        "(?i:token=)(?P<Value>[a-z]+)",  # a group name is syntax, not matched text
    ],
)
def test_a_pattern_that_matches_the_same_once_lowered_still_loads(pattern):
    assert [name for name, _ in scan.parse_shapes(f"same_lowered\t{pattern}\n")] == [
        "same_lowered"
    ]


@pytest.mark.parametrize(
    "pattern",
    [
        # Fix round 5: the eight shapes the round-4 review got past the guard.
        # Each loaded, its original pattern matched a body, and scan() found
        # nothing on that body.
        "(?i:contraseña)=\\S+",  # non-ASCII: `.lower()` folds ñ, the lowered copy keeps Ñ
        "(?i:İd)=\\S+",  # non-ASCII: İ lowercases to two code points
        "x(?i:a\\))?=\\S+",  # a quantifier after a group whose body holds `\)`
        "pw(?i:\\(old\\))?=\\S+",  # the same
        "(?x)x(?i:tok) ?=\\S+",  # verbose: a space before the quantifier
        "x(?i:tok)(?#c)?=\\S+",  # a comment before the quantifier
        "secret[(?i:=)]\\S+",  # a `(?i:` inside a character class
        "(?:\\(?i:\\)x)=\\S+",  # a `(?i:` after an escaped paren
    ],
)
def test_every_guard_bypass_the_round_4_review_built_is_refused(pattern):
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes(f"bypass\t{pattern}\n")


@pytest.mark.parametrize(
    "pattern",
    [
        "(?i:token=)ñ",  # non-ASCII outside the group: Ñ in the body never lowers to it
        "(?i:token=)\\S+(?-x:a)",  # any inline flag set naming x
        "(?ix:a)(?i:token=)",  # the same, combined with i
        "\\\\(?i:token=)",  # a `(?i:` straight after a backslash, even an escaped one
        "(?i:token=)\\(?P<Abc>",  # not a group name: `\(` is a literal paren, P and A are text
        "(?i:token=)[\\(?P<A>]",  # nor inside a class
    ],
)
def test_the_rest_of_the_pattern_is_read_as_re_reads_it_not_textually(pattern):
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes(f"textual\t{pattern}\n")


@pytest.mark.parametrize(
    ("pattern", "body"),
    [
        # Folded to bare text, `{1,` and `2` and `}` became the quantifier
        # `{1,2}`; folded into a non-capturing group they stay literal text.
        ("x{1,(?i:2)}y=", "x{1,2}y=hunter2fake"),
        ("x{(?i:2)}y=", "x{2}y=hunter2fake"),
        # A non-ASCII letter in the body is left alone by the lowering, so a
        # negated class still matches it: `.lower()` would turn the Kelvin
        # sign into an ASCII `k` that `[^A-Za-z]` refuses.
        ("(?i:token=)[^A-Za-z]", "TOKEN=\u212a"),
        ("(?i:token=)[^A-Za-z]", "token=\u0130"),
    ],
)
def test_the_fast_path_finds_what_the_original_pattern_finds(monkeypatch, pattern, body):
    shapes = scan.parse_shapes(f"probe\t{pattern}\n")
    [(_, compiled)] = shapes
    assert compiled.search(body)
    monkeypatch.setattr(scan, "SHAPES", shapes)
    assert [f.shape for f in scan.scan(body)] == ["probe"]


@pytest.mark.parametrize(
    "pattern",
    [
        "(?i:password=)(?!changeme)\\S+",  # `Changeme...` is excluded once lowered, not before
        "(?i:password=)(?<!x)\\S+",
        "(?i:token=)(?!(?:abc|def))\\w+",
    ],
)
def test_a_negative_lookaround_in_a_case_insensitive_shape_is_rejected(pattern):
    # A negative lookaround says what must NOT be there, so text it misses
    # on the original body but matches on the lowercased copy takes a real
    # match away: `(?!changeme)` lets `PASSWORD=Changeme2026!Xq9fake`
    # through on the original and excludes it once lowered.
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes(f"lookaround\t{pattern}\n")


def test_a_positive_lookaround_in_a_case_insensitive_shape_still_loads():
    pattern = "(?i:token=)(?=[a-z0-9]{8})\\S+"
    assert [name for name, _ in scan.parse_shapes(f"lookahead\t{pattern}\n")] == ["lookahead"]


def test_the_packaged_shapes_file_passes_every_load_time_guard():
    text = (scan.resources.files("artifacts") / "secret_shapes.txt").read_text(encoding="utf-8")
    assert [name for name, _ in scan.parse_shapes(text)] == [name for name, _ in scan.SHAPES]
    assert len(scan.SHAPES) == 22


# --- performance: each view is scanned once per shape, not once per line
# per shape, and results are bounded regardless of match density. ----------


def test_a_megabyte_of_newlines_scans_in_under_two_seconds():
    body = "\n" * 1_000_000
    start = time.perf_counter()
    scan.scan(body)
    # Generous for the same reason as the dense-body case below: a wall clock
    # on a shared runner measures the runner.
    assert time.perf_counter() - start < 10.0


def test_a_dense_single_line_body_gives_a_small_bounded_result_quickly():
    # Before bounding, one Finding per MATCH (not per line) meant a 1 MB
    # single-line body dense with hits produced on the order of 200k
    # Finding objects in about a second -- Task 5's 422 echoes every shape
    # name, so that became several MB of response body for one publish.
    body = "?token=1&password=1&secret=1" * 40_000  # ~1.1 MB, one line
    start = time.perf_counter()
    findings = scan.scan(body)
    elapsed = time.perf_counter() - start
    # THE COUNT is the regression, and it is asserted on the next line. The
    # clock is only a smoke check against a catastrophic blow-up, so it is
    # generous on purpose: at 1.0s this failed on a box at load average 12.6
    # while passing in 0.35s idle, which makes it a test of how busy the
    # machine is. That matters more than it sounds — a shared CI runner is
    # busiest exactly when someone is pushing a fix, so a tight wall clock
    # blocks the deploy at the worst possible moment and blames the code.
    assert elapsed < 10.0, f"took {elapsed:.2f}s — not merely slow, something blew up"
    assert 0 < len(findings) <= scan._MAX_FINDINGS_TOTAL
    # A single-line body: de-duplication to (shape, line) must have
    # collapsed tens of thousands of matches per shape down to one each.
    assert all(f.line == 1 for f in findings)


def test_a_match_repeated_on_many_distinct_lines_is_capped_per_shape():
    body = "token=fakevalue1234567890\n" * 100_000
    start = time.perf_counter()
    findings = scan.scan(body)
    elapsed = time.perf_counter() - start
    # The CAP below is the regression. Generous clock: see the dense-body test.
    assert elapsed < 10.0, f"took {elapsed:.2f}s — something blew up"
    token_findings = [f for f in findings if f.shape == "token_assignment"]
    assert 0 < len(token_findings) <= scan._MAX_LINES_PER_SHAPE


# --- every shape gets a positive fake it must catch and a near-miss it must
# not. --------------------------------------------------------------------

SHAPE_FIXTURES: dict[str, tuple[str, str]] = {
    "authorization_header": (
        "Authorization: Bearer FAKETOKEN123",
        "Authorization required for this page",
    ),
    "bearer_token": (
        "Bearer FAKETOKEN1234567890AB",
        "the bearer of this token must present it",
    ),
    "password_assignment": ("password=FAKEPASS123", "passwordless login is enabled"),
    "passwd_assignment": ("passwd=fakepw123", "passwdgen is a tool name"),
    "pass_assignment": ("pass=fakepw123", "password=fake123"),
    "api_key_literal": ("api_key=FAKEKEY123", "our api key rotation policy"),
    "apikey_literal": ("apikey=FAKEKEY123", "the API key rotation policy"),
    "token_assignment": ("token=FAKETOKEN123", "tokenize the input string"),
    "secret_assignment": ("secret=FAKESECRET123", "secretary meeting notes"),
    "x_auth_token_header": ("X-Auth-Token: FAKEVALUE123", "X Auth Token missing entirely"),
    "age_encrypted_header": (
        "-----BEGIN AGE ENCRYPTED FILE-----\nFAKE\n-----END AGE ENCRYPTED FILE-----",
        "beginning age-encrypted formats are old news",
    ),
    "pgp_private_key_header": (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nFAKE\n-----END PGP PRIVATE KEY BLOCK-----",
        "begin pgp private conversation",
    ),
    "openssh_private_key_header": (FAKE_PRIVATE_KEY, "openssh private key rotation guide"),
    "rsa_private_key_header": (
        "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----",
        "rsa private key rotation guide",
    ),
    "ec_private_key_header": (
        "-----BEGIN EC PRIVATE KEY-----\nFAKE\n-----END EC PRIVATE KEY-----",
        "ec private key rotation guide",
    ),
    "private_key_field": ("PrivateKey = FAKEBASE64KEYDATA", "PrivateKeystore uses AES-256"),
    "preshared_key_field": ("PresharedKey = FAKEBASE64KEYDATA", "PresharedKeys rotate weekly"),
    "credential_set_literal": (
        "the credential-set fake-creds-01 was rotated",
        "credentials are set nightly by the job",
    ),
    "url_query_secret": (
        "https://example.test/page?token=FAKETOKEN123&other=1",
        "https://example.test/page?key=",
    ),
    "url_inline_credentials": (
        "redis://user:FAKEPASSWORD123@cache.internal:6379/0",
        "https://example.test/path",
    ),
    "aws_access_key_id": (FAKE_AWS, "AKIA" + "SHORT"),
    "private_key_header": (
        "-----BEGIN DSA PRIVATE KEY-----\nFAKE\n-----END DSA PRIVATE KEY-----",
        "-----BEGIN CERTIFICATE-----",
    ),
}


def test_every_shape_name_has_a_fixture():
    assert set(SHAPE_FIXTURES) == {name for name, _ in scan.SHAPES}


@pytest.mark.parametrize("name", sorted(SHAPE_FIXTURES))
def test_shape_catches_its_fake_and_ignores_its_near_miss(name):
    positive, near_miss = SHAPE_FIXTURES[name]
    caught = scan.scan(positive)
    assert any(f.shape == name for f in caught), f"{name} did not fire on its own positive fake"
    missed = scan.scan(near_miss)
    assert not any(f.shape == name for f in missed), f"{name} fired on its near-miss"


def test_url_query_secret_stays_case_sensitive():
    assert scan.scan("https://example.test/page?Key=FAKETOKEN123") == []


def test_bearer_token_requires_a_token_shaped_value_not_just_the_word():
    # Round 1 kept this shape case-sensitive to dodge prose ("the bearer of
    # this token..."), reasoning that RFC-correctness made the scheme name
    # case-sensitive -- wrong, RFC 9110 SS11.1 makes auth schemes
    # case-insensitive on the wire. Blanket case-insensitivity alone then
    # fired on 32 of 214 homelab docs files. The value-shape requirement
    # (16+ base64url-ish characters) is what actually distinguishes a
    # credential from prose, not case, and it catches JSON/dict forms too.
    assert scan.scan("the bearer of this token must present it") == []
    assert scan.scan("Bearer FAKETOKEN1234567890AB") != []
    assert scan.scan("bearer FAKETOKEN1234567890AB") != []  # lowercase scheme
    assert scan.scan('{"authorization": "bearer FAKETOKEN1234567890AB"}') != []


# --- the two BRE-translated shapes: proof the translation was necessary,
# not just claimed. -----------------------------------------------------


def test_pasting_either_untranslated_bre_source_back_in_would_go_undetected(monkeypatch):
    bre_sources = {
        "url_query_secret": r"[?&]\(api_\)\?\(key\|token\|secret\|password\)=[^&[:space:]]",
        "url_inline_credentials": r"://[^/[:space:]:]\+:[^/[:space:]@]\+@",
    }
    patched = [
        (name, re.compile(bre_sources[name]) if name in bre_sources else pattern)
        for name, pattern in scan.SHAPES
    ]
    monkeypatch.setattr(scan, "SHAPES", patched)
    findings = scan.scan(
        "?token=FAKETOKEN123 and redis://user:FAKEPASSWORD123@cache.internal:6379/0"
    )
    assert not any(f.shape in bre_sources for f in findings)


# --- case-insensitive key-name shapes: the commonest real leak class -------


@pytest.mark.parametrize(
    ("body", "expected_shape"),
    [
        ("DB_PASSWORD=hunter2fake", "password_assignment"),
        ("GITHUB_TOKEN=ghp_FAKETOKEN123", "token_assignment"),
        ("OPENAI_API_KEY=sk-FAKEKEY123", "api_key_literal"),
    ],
)
def test_uppercase_environment_variable_dumps_are_caught(body, expected_shape):
    findings = scan.scan(body)
    assert any(f.shape == expected_shape for f in findings)


# --- placeholder values are excluded only for assignment-style shapes,
# fullmatch not prefix, and real-looking values are never mistaken for one.


@pytest.mark.parametrize(
    "body",
    [
        "password=<YOUR_PASSWORD_HERE>",
        "token=${GITHUB_TOKEN}",
        "secret={{ vault_secret }}",
        "password=********",
        "password=xxxxxxxx",
        "password=changeme",
        "token=REDACTED",
    ],
)
def test_placeholder_values_are_excluded_for_assignment_shapes(body):
    assert scan.scan(body) == []


def test_placeholder_exclusion_does_not_apply_to_non_assignment_shapes():
    # api_key_literal's match is just the bare key name -- it never has a
    # value to classify, so unlike password=/token=/secret=, a
    # placeholder-looking tail must not suppress it (fix round 1's
    # regression applied placeholder logic to every shape, not just
    # assignment ones).
    assert scan.scan("api_key=example") != []


def test_an_assignment_with_no_value_at_all_is_treated_as_a_placeholder():
    assert scan.scan("password=") == []
    assert scan.scan("password=\n") == []


def test_a_realistic_looking_value_is_not_mistaken_for_a_placeholder():
    assert scan.scan("password=hunter2fake123") != []


def test_a_placeholder_body_password_wrapped_in_a_url_still_flags_the_url_shape():
    findings = scan.scan("password=<REDACTED>\ntoken=FAKETOKEN123")
    assert any(f.shape == "token_assignment" for f in findings)
    # Refused on purpose (fix round 4): `<REDACTED>` is a tag to the text view,
    # exactly like `<br>`, so `password=` there takes its value from the next
    # line. A browser hides an unescaped `<REDACTED>` anyway; a visible
    # placeholder is written `&lt;REDACTED&gt;`, which stays excluded.
    assert any(f.shape == "password_assignment" for f in findings)


def test_an_escaped_angle_placeholder_followed_by_more_lines_is_still_excluded():
    assert scan.scan("DB_PASSWORD=&lt;YOUR_DB_PASSWORD&gt;\nDB_HOST=localhost") == []


def test_a_bare_secret_with_nothing_after_it_still_fires():
    assert scan.scan(FAKE_AWS) != []
    assert scan.scan(FAKE_PRIVATE_KEY) != []


@pytest.mark.parametrize(
    "value",
    [
        "changeme2026!Xq9",
        "examplePassw0rd!",
        "xxxhunter2fake",
        "XXXX-real-fake-9f8e",
    ],
)
def test_a_value_merely_starting_like_a_placeholder_is_not_excluded(value):
    # A prefix match (fix round 1's other defect) wrongly excluded any value
    # that merely STARTS with changeme/example/a run of x -- these are
    # real-looking values, not placeholders, and must fullmatch to exclude.
    assert scan.scan(f"password={value}") != []


# --- HTML markup around a credential must not hide it: two views (raw body,
# tag-stripped/entity-unescaped text) are merged. ---------------------------


@pytest.mark.parametrize(
    ("body", "expected_shape"),
    [
        (f"<td>{FAKE_AWS}</td>", "aws_access_key_id"),
        (f"<code>{FAKE_AWS}</code>", "aws_access_key_id"),
        (f"{FAKE_AWS}<br>", "aws_access_key_id"),
        (f"key id: {FAKE_AWS} <span>", "aws_access_key_id"),
        ("<pre>-----BEGIN PRIVATE KEY-----<br>MIIEfakebase64data", "private_key_header"),
        ("<pre>-----BEGIN PRIVATE KEY-----</pre>", "private_key_header"),
        ("<p>password=<b>hunter2fake</b></p>", "password_assignment"),
        ("api_key<br>sk-proj-fakekey123", "api_key_literal"),
        ("X-Auth-Token: <code>fakevalue123</code>", "x_auth_token_header"),
        ("Authorization: <code>Basic ZmFrZWJhc2ljZmFrZQ==</code>", "authorization_header"),
        ("<td>DB_PASSWORD=</td><td>hunter2fake</td>", "password_assignment"),
        ("<dt>OPENAI_API_KEY</dt><dd>sk-proj-fake123</dd>", "api_key_literal"),
        ("password=<PASSWORD> actually it is hunter2fake", "password_assignment"),
        ("password=${PW}hunter2fake", "password_assignment"),
    ],
)
def test_a_credential_next_to_or_split_by_markup_is_still_caught(body, expected_shape):
    findings = scan.scan(body)
    assert any(f.shape == expected_shape for f in findings), (
        f"expected {expected_shape} to fire on markup-adjacent fake"
    )


@pytest.mark.parametrize(
    ("body", "expected_shape"),
    [
        ("redis://default:changeme-prod-fake@db.example.test:6379", "url_inline_credentials"),
        ("redis://default:xxxYfakeZ@db.example.test:6379", "url_inline_credentials"),
        ("?token=example_live_fake123", "url_query_secret"),
    ],
)
def test_a_placeholder_look_alike_url_value_with_a_real_suffix_still_fires(body, expected_shape):
    findings = scan.scan(body)
    assert any(f.shape == expected_shape for f in findings)


def test_a_pkcs8_header_split_across_html_elements_is_still_caught():
    findings = scan.scan("<pre>-----BEGIN PRIVATE KEY-----</pre><pre>MIIEfakebase64</pre>")
    assert any(f.shape == "private_key_header" for f in findings)


def test_a_text_view_only_hit_is_placed_with_the_text_views_own_newlines():
    # Only the text view sees `<b>password</b>=...`, and deleting two long
    # tags puts that match at a text-view offset smaller than the raw body's
    # first newline. Looked up in the raw view's newline table it would be
    # reported on line 1; each view must use its own table. The raw-view
    # `Authorization:` hit (the first shape scanned) makes the raw table the
    # first one built, so a shared table would be the wrong one.
    long_tag = '<p class="' + "x" * 200 + '">'
    body = f"{long_tag}Authorization: a</p>\n{long_tag}b</p>\n<b>password</b>=hunter2fake"
    findings = scan.scan(body)
    assert [f.line for f in findings if f.shape == "password_assignment"] == [3]


def test_detagging_never_changes_line_numbers_the_raw_body_would_report():
    # A tag replaces to the same number of newlines it contained, so a
    # credential on line 3 of the raw body is still reported as line 3, not
    # shifted by however much markup surrounds it.
    body = "one\ntwo\n<div>\n  password=FAKEPASS123\n</div>\nfive"
    findings = scan.scan(body)
    assert any(f.shape == "password_assignment" and f.line == 4 for f in findings)


# --- fix round 3: a stray `<` with no later `>` must not make the tag
# matcher quadratic. ---------------------------------------------------


def test_a_stray_angle_bracket_with_no_closing_bracket_scans_quickly():
    # `x <- 1` (R assignment) has a `<` on every line and never a `>` at
    # all. Under the old `<[^>]*>` tag pattern this was quadratic: every
    # `<` made the engine scan to the end of the body hunting for a `>`
    # that never comes. A clean 1 MiB body of this shape cost 11.1s under
    # the old pattern; this must stay well under a second.
    body = "x <- 1\n" * 150_000  # ~1 MiB, no ">" anywhere
    start = time.perf_counter()
    findings = scan.scan(body)
    elapsed = time.perf_counter() - start
    # Unlike the other timings here, THIS clock is the test: the regression is
    # quadratic backtracking, which cost 11.1s on this body. 5s still separates
    # linear from quadratic by a wide margin while surviving a loaded runner —
    # a 1s bound was measuring the machine, and a 10s bound would no longer
    # catch the 11.1s case it exists for.
    assert elapsed < 5.0, f"took {elapsed:.2f}s — suspect quadratic backtracking"
    assert findings == []


# --- fix round 3: the raw-match safety valve must count only real matches,
# never placeholders, or a flood of placeholders hides a real secret behind
# it. --------------------------------------------------------------------


def test_a_real_secret_behind_500_placeholder_lines_is_still_caught():
    placeholder_lines = "DB_PASSWORD=${DB_PASSWORD}\n" * 500
    body = placeholder_lines + "DB_PASSWORD=hunter2fake\n"
    findings = scan.scan(body)
    hits = [f for f in findings if f.shape == "password_assignment"]
    assert hits, "the real secret after 500 placeholders was not found"
    assert all(f.line == 501 for f in hits)


# --- fix round 3: deleting a tag (not blanking it with whitespace) must
# reunite a token markup splits through its middle. -------------------------


@pytest.mark.parametrize(
    ("body", "expected_shape"),
    [
        (
            (
                '<span class="na">DB_PASSWORD</span><span class="o">=</span>'
                '<span class="s">hunter2fake</span>'
            ),
            "password_assignment",
        ),
        ("<code>password</code>=hunter2fake", "password_assignment"),
        (f"{FAKE_AWS[:4]}<wbr>{FAKE_AWS[4:]}", "aws_access_key_id"),
        (f"{FAKE_AWS[:4]}&shy;{FAKE_AWS[4:]}", "aws_access_key_id"),
        ("redis://u:<b>pw</b>@h", "url_inline_credentials"),
    ],
)
def test_a_token_split_through_its_middle_by_markup_is_still_caught(body, expected_shape):
    findings = scan.scan(body)
    assert any(f.shape == expected_shape for f in findings), (
        f"expected {expected_shape} to fire on a token split by markup"
    )


def test_soft_hyphen_and_zero_width_characters_are_stripped_from_the_text_view():
    zero_width_space = chr(0x200B)
    body = f"password{zero_width_space}=hunter2fake"
    assert scan.scan(body) != []


# --- fix round 3 (proof, not assumption): deleting tags glues adjacent
# cells together in the text view -- does that ever cost a match a format-
# strict shape would otherwise have made? -----------------------------------


def test_two_aws_keys_glued_together_by_tag_deletion_are_still_both_findable():
    # <td>AKIA...</td><td>AKIA...</td> becomes one glued run in the text
    # view once tags are deleted. aws_access_key_id's pattern is
    # AKIA[0-9A-Z]{16} -- an EXACT 16-character class, not open-ended -- so
    # the first match consumes exactly 20 characters and finditer's next
    # search position lands exactly on the second key's leading "AKIA",
    # with no `\b` involved at all. The raw view never glues anything in
    # the first place: it still sees `>AKIA...<` with both keys' boundaries
    # intact. Both routes should mean detection on this line is not lost.
    body = f"<td>{FAKE_AWS}</td><td>{FAKE_AWS}</td>"
    findings = scan.scan(body)
    assert any(f.shape == "aws_access_key_id" for f in findings), (
        "aws_access_key_id did not fire at all on two keys glued by tag deletion"
    )


# --- fix round 3: the case-insensitive fast path must lowercase with a
# length-preserving table, never str.lower(), or an offset shifts. ----------


def test_a_length_changing_uppercase_letter_earlier_in_the_body_does_not_corrupt_offsets():
    # U+0130 (LATIN CAPITAL LETTER I WITH DOT ABOVE) lowercases via
    # str.lower() to TWO code points (i + combining dot above) -- exactly
    # the length-changing transform the case-insensitive fast path must not
    # use, since it would shift every offset after it in a lowered copy
    # relative to the original body, corrupting both the line number
    # reported for a later match and the value read back for placeholder
    # classification.
    turkish_i = chr(0x0130)
    body = f"{turkish_i}stanbul is a city\npassword=hunter2fake123\ntoken=${{TOKEN}}"
    findings = scan.scan(body)
    password_hit = next(f for f in findings if f.shape == "password_assignment")
    assert password_hit.line == 2
    assert not any(f.shape == "token_assignment" for f in findings)


# --- fix round 3: a body with nothing for the text view to strip should not
# pay for a second scan pass. ------------------------------------------------


def test_a_body_with_no_markup_or_entities_does_not_duplicate_findings_via_a_second_view():
    # If the text view were built and scanned even when identical to the
    # raw view, a match would still only be de-duplicated to one line
    # (Finding de-dup is per shape+line, not per view) -- so this cannot
    # observe the skip directly through finding counts. It exists as a
    # behavioural pin: a plain-text body's result is unaffected by whether
    # the (skipped) second view would have been scanned.
    body = "password=hunter2fake123\ntoken=anotherfaketoken456"
    findings = scan.scan(body)
    assert any(f.shape == "password_assignment" for f in findings)
    assert any(f.shape == "token_assignment" for f in findings)


# --- fix round 4: an assignment whose value starts on the next line still
# has a value. With nothing after the operator on its own line, the first
# token of the next non-blank line (at most 3 lines ahead) is classified. ---


@pytest.mark.parametrize(
    ("body", "expected_shape"),
    [
        ("password=\nhunter2fake", "password_assignment"),
        ("DB_PASSWORD=\r\nhunter2fake", "password_assignment"),
        ('const password=\n  "hunter2fake";', "password_assignment"),
        ("token=\x0cfakeTOK9fakeFAKE", "token_assignment"),
        ("DB_PASSWORD=<br>\nhunter2fake", "password_assignment"),
        ("password=\n\n\nhunter2fake", "password_assignment"),  # 3 lines down: in range
        ("password= \nhunter2fake", "password_assignment"),  # NBSP is nothing
        ("password= \t　'\nhunter2fake", "password_assignment"),  # so is this mix
    ],
)
def test_a_value_on_the_next_line_is_classified_not_dropped(body, expected_shape):
    findings = scan.scan(body)
    assert [f.line for f in findings if f.shape == expected_shape] == [1]


@pytest.mark.parametrize(
    "body",
    ["password=\n${PASSWORD}", "password=\n{{ vault_pw }}", "password=\n\n  '${PASSWORD}'"],
)
def test_a_placeholder_on_the_next_line_is_still_a_placeholder(body):
    assert scan.scan(body) == []


@pytest.mark.parametrize(
    ("body", "line"),
    [
        # A plain-text value four lines down: round 4 accepted missing this,
        # and the same change closes it.
        ("password=\n\n\n\nhunter2fake", 1),
        ("password=\n\n\n\n\n\n\n\n\n\nhunter2fake", 1),
        # Every deleted tag leaves the blank line it stood on, so markup
        # alone puts a value further down than any line count would allow.
        # These five are the reviewer's table, verbatim: the first two were
        # caught before this commit, the last three were published clean.
        ("<tr>\n<td>DB_PASSWORD=</td>\n<td>hunter2fake</td>", 2),
        ("<td>DB_PASSWORD=</td>\n<td>\n<code>\nhunter2fake", 1),
        ("<td>DB_PASSWORD=</td>\n<td>\n<div>\n<code>\nhunter2fake", 1),
        ("<dl>\n<dt>API_TOKEN=</dt>\n<dd>\n<pre>\n<code>\nfakeTOK9fakeTOK9", 2),
        ("<td>token=</td>\n</tr>\n<tr>\n<td>\nfakeTOK9fakeTOK9", 1),
        # Indented markup, and a run of void elements, reach the same place.
        (
            (
                "<dl>\n  <dt>DB_PASSWORD=</dt>\n  <dd>\n    <code>\n"
                "      hunter2fake\n    </code>\n  </dd>\n</dl>"
            ),
            2,
        ),
        (
            (
                "<table>\n<tr><td>API_TOKEN=</td></tr>\n<tr>\n<td>\n"
                "fakeTOK9fakeTOK9\n</td>\n</tr>\n</table>"
            ),
            2,
        ),
        ("<p>password=</p>\n<br>\n<br>\n<br>\n<br>\n<span>hunter2fake</span>", 1),
    ],
)
def test_a_value_any_number_of_blank_lines_down_is_still_classified(body, line):
    # Blank lines are skipped, not counted: what ends the lookahead is the
    # first line with something on it, not a line budget.
    assert [f.line for f in scan.scan(body) if f.shape.endswith("_assignment")] == [line]


@pytest.mark.parametrize(
    "body",
    [
        "password=\n\n\n\n${PASSWORD}",
        "password=\n\n\n\n\n\n  '{{ vault_pw }}'",
        "<td>DB_PASSWORD=</td>\n<td>\n<code>\n&lt;YOUR_PASSWORD&gt;",
        "password=\n\n\n\n",
    ],
)
def test_a_placeholder_any_number_of_blank_lines_down_is_still_a_placeholder(body):
    assert scan.scan(body) == []


@pytest.mark.parametrize(
    "body",
    [
        "password= hunter2fake",
        "password=\thunter2fake",
        "password= hunter2fake",  # NBSP: missed at c2ff307
        "<p>password=&nbsp;hunter2fake</p>",  # NBSP as an entity: missed at c2ff307
        "password=　hunter2fake",  # ideographic space: missed at c2ff307
        'password="hunter2fake"',
        "password= ' hunter2fake'",
        "password=" + " " * 250 + "hunter2fake",  # past the old 200-character window
    ],
)
def test_whitespace_or_a_quote_before_a_value_does_not_hide_it(body):
    # Whitespace is "nothing" whatever its kind, before a value on the same
    # line just as before a line break.
    assert any(f.shape == "password_assignment" for f in scan.scan(body))


def test_a_flood_of_empty_assignments_each_looking_past_blank_lines_scans_quickly():
    # Every match skips four blank lines to reach the next key, in both
    # views: the leading `&amp;` makes the text view differ. Each blank run
    # is skipped once, by one match, so the work stays linear however far a
    # value sits from its key.
    body = "&amp;\n" + "pass=\n\n\n\n" * 116_000  # ~1 MiB
    start = time.perf_counter()
    findings = scan.scan(body)
    elapsed = time.perf_counter() - start
    assert len(findings) <= scan._MAX_FINDINGS_TOTAL
    # The cap above is the regression. Generous clock: see the dense-body test.
    assert elapsed < 10.0, f"took {elapsed:.2f}s — something blew up"


# --- fix round 5: a value that begins with `&`, `;` or `,` is a value. Only
# `&` starting the next query parameter (`&name=`, `&amp;name=`) or closing
# the query, and `;`/`,` with nothing after them, leave it empty. -----------


@pytest.mark.parametrize(
    ("body", "expected_shape"),
    [
        ("DB_PASSWORD=&k2Lq9fakeXv", "password_assignment"),
        ("DB_PASSWORD='&k2Lq9fakeXv'", "password_assignment"),
        ("<pre>DB_PASSWORD=&amp;k2Lq9fakeXv</pre>", "password_assignment"),
        ("DB_PASSWORD=;k2Lq9fakeXv", "password_assignment"),
        ("DB_PASSWORD=,k2Lq9fakeXv", "password_assignment"),
        ("API_TOKEN=&k2Lq9fakeXv", "token_assignment"),
        ("client_secret=;k2Lq9fakeXv", "secret_assignment"),
        ("password=\n&hunter2fake", "password_assignment"),
        # Inside an attribute only the raw view sees it: the text view
        # deletes the whole tag.
        ('<input value="password=&amp;k2Lq9fakeXv">', "password_assignment"),
        ('<div data-env="password=&quot;hunter2fake&quot;">', "password_assignment"),
    ],
)
def test_a_value_starting_with_an_ampersand_semicolon_or_comma_is_classified(
    body, expected_shape
):
    findings = scan.scan(body)
    assert [f.line for f in findings if f.shape == expected_shape] == [1]


@pytest.mark.parametrize(
    "body",
    [
        "https://x.invalid/?password=&next=1",  # an empty query parameter
        '<a href="https://x.invalid/?token=&amp;next=1">x</a>',  # the same, escaped
        "&amp;token=&amp;",  # an empty last parameter, escaped
        '<a href="https://x.invalid/?token=&">x</a>',  # a trailing `&`
        "pass=;",  # a statement with an empty value
        "password=; set it in the vault",  # an INI-style comment after nothing
        "DB_PASSWORD=,\nDB_USER=,",
        # html.escape() output: quotes and angle brackets arrive as entities
        # in the raw view, and the value inside them is still a placeholder.
        "<code>password = &quot;changeme&quot;</code>",
        "<code>password=&#x27;${DB_PASSWORD}&#x27;</code>",
        "<code>password=&quot;&lt;YOUR_PASSWORD&gt;&quot;</code>",
        "password=&nbsp;${DB_PASSWORD}",
    ],
)
def test_an_empty_parameter_or_an_escaped_placeholder_stays_clean(body):
    assert scan.scan(body) == []


@pytest.mark.parametrize(
    "body",
    [
        # The visible placeholder form in a table or a code element: the raw
        # view read `</code>` as part of the value once `&` stopped meaning
        # "empty", so these were refused.
        "<code>API_TOKEN=&lt;your-token&gt;</code>",
        "<td>DB_PASSWORD=&lt;YOUR_DB_PASSWORD&gt;</td>",
        "<p>password=&nbsp;${DB_PASSWORD}</p>",
        # The same, unescaped: refused before fix round 5 as well.
        "<td>password=${DB_PASSWORD}</td>",
        "<td>password=changeme</td>",
        # An empty value closed by a tag: the tag is not the value.
        "<td>DB_PASSWORD=</td>",
        '<td>password=""</td>',
        "<td>password=&quot;&quot;</td>",
    ],
)
def test_a_tag_straight_after_a_placeholder_is_not_part_of_it(body):
    assert scan.scan(body) == []


@pytest.mark.parametrize(
    "body",
    [
        "<td>password=hunter2fake</td>",
        "<code>password=${PW}hunter2fake</code>",
        "<td>password=changeme<span>9fakeXv</span></td>",  # the text view joins them
    ],
)
def test_a_real_value_next_to_a_tag_is_still_caught(body):
    assert any(f.shape == "password_assignment" for f in scan.scan(body))


# --- fix round 5: a body with one non-ASCII character anywhere is lowered
# and stripped as fast as an ASCII one, and still scanned the same. ---------


@pytest.mark.parametrize(
    "body",
    [
        "\U0001f600\npassword=hunter2fake",  # stored four bytes per character
        "caf\u00e9\npassword=hunter2fake",  # one byte per character, not ASCII
        "\u2013\npassword=hunter2fake",  # two bytes per character
        "\ud800\npassword=hunter2fake",  # a lone surrogate, as JSON can deliver one
        "\U0001f600\npassword\u200b=hunter2fake",  # a zero-width space to strip
        "\U0001f600\nPASS\u00ad=hunter2fake",  # a soft hyphen to strip
    ],
)
def test_a_non_ascii_body_is_scanned_the_same(body):
    findings = scan.scan(body)
    assert [f.line for f in findings if f.shape.endswith("_assignment")] == [2]


@pytest.mark.parametrize(
    "text",
    ["PASSWORD=Xy", "\u0130STANBUL", "\u212aEY=", "\ud800A\udfffB", "\U0001f600Z\n", "CAF\u00c9"],
)
def test_the_lowered_copy_lowers_a_to_z_and_nothing_else(text):
    lowered = scan._lower_ascii(text)
    assert lowered == text.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
    assert len(lowered) == len(text)


# --- fix round 5, final commit: a space written as an HTML character
# reference is a space, in every form. ---------------------------------------

SKIPPED_REFERENCES = [
    "&ensp;",  # every one of these refused a placeholder before this commit
    "&emsp;",
    "&thinsp;",
    "&#32;",
    "&Tab;",
    "&MediumSpace;",
    "&nbsp;",
    "&nbsp",  # html.unescape reads the semicolon-less form too
    "&#xA0;",  # hex, either case
    "&#xa0;",
    "&#8203;",  # a zero-width space is no more part of a value than a space is
    "&quot;",
    "&#39;",
]


@pytest.mark.parametrize("reference", SKIPPED_REFERENCES)
@pytest.mark.parametrize(
    ("wrap", "line"), [("{}", 1), ("<td>{}</td>", 1), ('<div data-x="{}">', 1)]
)
def test_an_escaped_space_before_a_placeholder_does_not_make_it_a_value(reference, wrap, line):
    assert scan.scan(wrap.format(f"password={reference}${{DB_PASSWORD}}")) == []


@pytest.mark.parametrize("reference", SKIPPED_REFERENCES)
def test_an_escaped_space_before_a_real_value_does_not_hide_it(reference):
    body = f"password={reference}hunter2fake"
    assert [f.line for f in scan.scan(body) if f.shape == "password_assignment"] == [1]


def test_every_html_space_or_quote_reference_is_skipped_before_a_value():
    # Completeness, not a list someone kept up to date: every named
    # reference in html.entities.html5 that stands for whitespace, a quote
    # or an invisible character must behave like the character it writes.
    import html.entities

    references = {
        f"&{name}" if name.endswith(";") else f"&{name};"
        for name, char in html.entities.html5.items()
        if char and all(c in scan._SKIPPABLE for c in char) and "\n" not in char
    }
    assert len(references) > 20
    missed = [r for r in references if scan.scan(f"password={r}hunter2fake") == []]
    refused = [r for r in references if scan.scan(f"password={r}${{DB_PASSWORD}}") != []]
    assert missed == []
    assert refused == []


def test_a_decimal_charref_split_by_tags_does_not_crash_once_detagged():
    """`&#1<b></b>1<b></b>1...` holds no long digit run in the raw body --
    the digits only become one contiguous run after `_detag` deletes the
    tags between them (task 5 fix round 2). Before the fix this reached
    `html.unescape` as a multi-thousand-digit decimal reference and raised
    inside CPython's int(str) conversion."""
    body = "&#" + "1<b></b>" * 2000 + "1"
    assert scan.scan(body) == []


def test_a_huge_decimal_charref_with_no_semicolon_does_not_crash():
    body = "&#" + "0" * 4301 + "7"
    assert scan.scan(body) == []


def test_exactly_4300_decimal_digits_does_not_crash():
    """4300 is CPython's own integer-string conversion limit -- exactly at
    it does not raise even unfixed, but the defang applies regardless of
    that boundary (no legitimate reference needs more than seven digits),
    so this pins that the fix does not regress the boundary case."""
    body = "&#" + "1" * 4300
    assert scan.scan(body) == []


def test_a_huge_hex_charref_does_not_crash_and_is_not_defanged():
    """Base 16 is exempt from CPython's integer-string conversion limit, so
    a long hex reference never crashed and must not be treated as the
    decimal case: `_defang_huge_decimal_charrefs` only matches `&#` followed
    directly by decimal digits, never `&#x`."""
    body = "&#x" + "1" * 17200 + ";"
    assert scan.scan(body) == []


def test_a_legitimate_short_decimal_charref_still_decodes():
    """The defang only catches runs longer than any real code point needs --
    an ordinary reference like `&#65;` (`A`) must still unescape and scan
    normally, not get swept up by a fix aimed at absurd inputs. `&#65;&#75;
    &#73;&#65;` spells AKIA."""
    findings = scan.scan("token &#65;&#75;&#73;&#65;IOSFODNN7EXAMPLE end")
    assert any(f.shape == "aws_access_key_id" for f in findings)


# --- fix round 3: the first version of the crash defang was fail-open on
# any zero-padded reference, silently losing detections instead of raising.
# -----------------------------------------------------------------------


def test_a_zero_padded_charref_inside_an_aws_key_is_still_found():
    """The regression that started fix round 3: `&#00000065;` is a
    perfectly legitimate, if unusually padded, reference to `A` -- the
    first defang counted its 8 digit characters, decided it was "too long",
    and defanged it, so `html.unescape` never decoded it and the key was
    never assembled in the text view. `AKI&#00000065;IOSFODNN7EXAMPLE`
    published clean at 201 before this fix."""
    findings = scan.scan("AKI&#00000065;IOSFODNN7EXAMPLE")
    assert any(f.shape == "aws_access_key_id" for f in findings)


def test_an_extremely_zero_padded_charref_is_still_found_and_does_not_crash():
    """Padding a legitimate reference with thousands of zeros must neither
    lose the detection (the fail-open regression) nor crash (the original
    bug) -- stripping leading zeros is a `str.lstrip`, never an `int()`
    conversion, so it costs the same whether padded with none or with
    4301."""
    padded = "&#" + ("0" * 4301) + "65;"
    findings = scan.scan("AKI" + padded + "IOSFODNN7EXAMPLE")
    assert any(f.shape == "aws_access_key_id" for f in findings)


def test_an_unpadded_charref_is_still_found():
    findings = scan.scan("AKI&#65;IOSFODNN7EXAMPLE")
    assert any(f.shape == "aws_access_key_id" for f in findings)


def test_more_than_seven_significant_digits_is_never_a_real_code_point():
    """8 significant digits, no leading zeros -- the smallest 8-digit
    number, 10,000,000, already exceeds 0x10FFFF (1,114,111), the highest
    real code point, so this can never be legitimate at any padding.
    Confirms the >7-significant branch still runs (does not crash, finds
    nothing new) now that it is reached only after zeros are stripped."""
    assert scan.scan("&#12345678") == []


def _zero_padded_charref(char: str, width: int = 12) -> str:
    return f"&#{ord(char):0{width}d};"


def _charref_encode(text: str, width: int = 12) -> str:
    """Every character of `text` written as its own zero-padded decimal
    character reference -- round-trips losslessly through `html.unescape`,
    so a shape that matches `text` must match this encoding of it too, once
    decoded."""
    return "".join(_zero_padded_charref(char, width) for char in text)


@pytest.mark.parametrize("name", sorted(SHAPE_FIXTURES))
def test_every_shape_still_fires_zero_padded_charref_encoded(name):
    """Makes the fix round 3 regression permanent: a unit test on one
    encoding (`aws_access_key_id`, unpadded `&#65;`) is exactly what let a
    24-detection, 8-shape regression through undetected. Every shape gets
    its own genuine positive fixture, re-encoded character-by-character as
    zero-padded charrefs -- a shape found in plain form and lost once
    encoded fails here."""
    positive, _ = SHAPE_FIXTURES[name]
    encoded = _charref_encode(positive)
    findings = scan.scan(encoded)
    assert any(f.shape == name for f in findings), (
        f"{name} matched its plain fixture but not the zero-padded-charref encoding of it"
    )


# --- fix round 4: the >7-significant defang branch was still `&`-initial,
# which fooled `url_query_secret`'s own lookahead into reading "no value at
# all". ---------------------------------------------------------------


def test_a_query_secret_with_a_defanged_run_right_after_the_key_is_still_found():
    """The exact reported regression: `?key=&#12345678MYREALTOKEN99` scanned
    completely clean and published 201, while the page rendered
    `?key=<U+FFFD>MYREALTOKEN99` -- a real token, just not the one this
    input started with."""
    findings = scan.scan("<p>https://example.test/p?key=&#12345678MYREALTOKEN99</p>")
    assert any(f.shape == "url_query_secret" for f in findings)


def test_a_defanged_run_alone_as_a_query_secrets_value_still_fires():
    findings = scan.scan("?token=&#99999999")
    assert any(f.shape == "url_query_secret" for f in findings)


@pytest.mark.parametrize(
    ("position", "body"),
    [
        ("before", "password=MYREALSECRET99&#12345678"),
        ("after", "password=&#12345678MYREALSECRET99"),
        ("beside", "note &#12345678 password=MYREALSECRET99 end"),
        ("on its own preceding line", "&#12345678\npassword=MYREALSECRET99"),
        ("on its own following line", "password=MYREALSECRET99\n&#12345678"),
    ],
)
def test_a_defanged_run_never_suppresses_a_real_credential_near_it(position, body):
    """The `&#` prefix the first attempt at this defang kept was the only
    thing stopping an all-`x` filler from `fullmatch`ing the placeholder
    valve (`_PLACEHOLDER`'s `[*xX]{3,}`) and being read as the WHOLE value
    -- dropping a real credential that happened to sit next to one. The
    fixed filler is plain mixed-case letters, which can never fullmatch
    that valve alone or concatenated with a real value on either side."""
    findings = scan.scan(body)
    assert any(f.shape == "password_assignment" for f in findings), position


_BIG_DEFANG_RUN = "&#12345678"  # 8 significant digits, no leading zeros -- never a real
# code point at any padding, so this is always the >7-significant, defanged branch.


@pytest.mark.parametrize(
    "name", sorted(scan._ASSIGNMENT_SHAPES | {"url_query_secret"})
)
def test_a_shape_anchored_on_an_operator_still_fires_with_a_defang_right_after_it(name):
    """Generalises the reported miss beyond the one shape the report used:
    every shape whose own match depends on what character comes right
    after an operator (`_ASSIGNMENT_SHAPES`, plus `url_query_secret`'s
    lookahead) gets the same construction -- a >7-significant defangable
    run sitting immediately after the operator, ahead of the rest of a
    genuine value -- so a narrower regression than this one cannot recur
    unnoticed. The reviewer's own sweep (4170 comparisons) found losses
    only here and only at this position; this is that finding made
    permanent."""
    positive, _ = SHAPE_FIXTURES[name]
    operator_end = positive.index("=") + 1
    injected = positive[:operator_end] + _BIG_DEFANG_RUN + positive[operator_end:]
    findings = scan.scan(injected)
    assert any(f.shape == name for f in findings), (
        f"{name} lost its match with a defanged run right after its operator"
    )


# --- standing property: the defang must behave exactly as `html.unescape`
# would if CPython's own integer-string conversion limit did not exist.
# Three bugs in a row (round 2's fail-open, fix3-1's `&`-initial replacement,
# and whatever the next rewriting mistake turns out to be) were all the same
# shape: a rewriting step upstream changed something a later stage depended
# on. A property test catches the SHAPE of that mistake without anyone
# first having to think of the specific example that demonstrates it. ------


def _control_scan(text, monkeypatch):
    """`scan.scan`, with the crash-preventing defang stubbed to a no-op and
    CPython's own integer-string conversion limit lifted for the call --
    the oracle for what the scanner would find if `html.unescape` simply
    never had a limit to hit. Only ever used here: lifting the limit
    process-wide is exactly the exposure this module exists to avoid in
    production, which is why the real fix cannot be "lift the limit"."""
    monkeypatch.setattr(scan, "_defang_huge_decimal_charrefs", lambda detagged: detagged)
    original_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        return scan.scan(text)
    finally:
        sys.set_int_max_str_digits(original_limit)


def _property_corpus() -> list[str]:
    corpus = []
    for positive, _ in SHAPE_FIXTURES.values():
        corpus.append(positive)
        corpus.append(_charref_encode(positive))
        if "=" in positive:
            operator_end = positive.index("=") + 1
            corpus.append(
                positive[:operator_end] + _BIG_DEFANG_RUN + positive[operator_end:]
            )
    corpus += [
        "&#" + "1<b></b>" * 2000 + "1",
        "&#" + "0" * 4301 + "7",
        "&#" + "1" * 4300,
        "&#x" + "1" * 17200 + ";",
        "AKI&#00000065;IOSFODNN7EXAMPLE",
        "AKI&#" + ("0" * 4301) + "65;IOSFODNN7EXAMPLE",
        "AKI&#65;IOSFODNN7EXAMPLE",
        "&#12345678",
        "&#12345678;",
        "&#99999999999999999999;",
        "&#1114111;",
        "&#1114112;",
        "<p>https://example.test/p?key=&#12345678MYREALTOKEN99</p>",
        "?api_key=&#123456789012",
        "password=MYREALSECRET99&#12345678",
        "password=&#12345678MYREALSECRET99",
        "note &#12345678 password=MYREALSECRET99 end",
    ]
    return corpus


def test_the_defang_matches_unescape_with_the_limit_lifted_over_the_corpus(monkeypatch):
    """This is the test that would have caught fix round 2's regression (a
    zero-padded reference silently lost) and fix round 3's first attempt
    (`url_query_secret` silently lost) without anyone first constructing
    the specific example that demonstrated either -- both showed up as an
    inequality here, not as a hand-picked failing case.

    `monkeypatch.undo()` after each body, not only at test teardown: the
    real scan for the NEXT body must run with the real defang restored,
    not with the previous iteration's stub for
    `_defang_huge_decimal_charrefs` still in place -- otherwise every
    corpus item after the first would compare the stub against itself.
    """
    mismatches = []
    for body in _property_corpus():
        real = {(f.shape, f.line) for f in scan.scan(body)}
        control = {(f.shape, f.line) for f in _control_scan(body, monkeypatch)}
        monkeypatch.undo()
        if real != control:
            mismatches.append((body, real, control))
    assert not mismatches, mismatches


def test_the_defang_matches_unescape_with_the_limit_lifted_over_random_bodies(monkeypatch):
    """Fuzzed rather than fixture-driven, over the alphabet a rewriting bug
    like fix round 2's or fix round 3's hides in: digits, `&#;`, markup and
    whitespace. Seeded so a failure is reproducible."""
    rng = random.Random(20260919)
    alphabet = "&#;0123456789xX<>/bp \né"
    mismatches = []
    for _ in range(2000):
        length = rng.randint(0, 60)
        body = "".join(rng.choice(alphabet) for _ in range(length))
        real = {(f.shape, f.line) for f in scan.scan(body)}
        control = {(f.shape, f.line) for f in _control_scan(body, monkeypatch)}
        monkeypatch.undo()
        if real != control:
            mismatches.append((body, real, control))
    assert not mismatches, mismatches[:5]
