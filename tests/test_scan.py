import re
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


# --- performance: each view is scanned once per shape, not once per line
# per shape, and results are bounded regardless of match density. ----------


def test_a_megabyte_of_newlines_scans_in_under_two_seconds():
    body = "\n" * 1_000_000
    start = time.perf_counter()
    scan.scan(body)
    assert time.perf_counter() - start < 2.0


def test_a_dense_single_line_body_gives_a_small_bounded_result_quickly():
    # Before bounding, one Finding per MATCH (not per line) meant a 1 MB
    # single-line body dense with hits produced on the order of 200k
    # Finding objects in about a second -- Task 5's 422 echoes every shape
    # name, so that became several MB of response body for one publish.
    body = "?token=1&password=1&secret=1" * 40_000  # ~1.1 MB, one line
    start = time.perf_counter()
    findings = scan.scan(body)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0
    assert 0 < len(findings) <= scan._MAX_FINDINGS_TOTAL
    # A single-line body: de-duplication to (shape, line) must have
    # collapsed tens of thousands of matches per shape down to one each.
    assert all(f.line == 1 for f in findings)


def test_a_match_repeated_on_many_distinct_lines_is_capped_per_shape():
    body = "token=fakevalue1234567890\n" * 100_000
    start = time.perf_counter()
    findings = scan.scan(body)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0
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
    assert not any(f.shape == "password_assignment" for f in findings)


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


def test_detagging_never_changes_line_numbers_the_raw_body_would_report():
    # A tag replaces to the same number of newlines it contained, so a
    # credential on line 3 of the raw body is still reported as line 3, not
    # shifted by however much markup surrounds it.
    body = "one\ntwo\n<div>\n  password=FAKEPASS123\n</div>\nfive"
    findings = scan.scan(body)
    assert any(f.shape == "password_assignment" and f.line == 4 for f in findings)
