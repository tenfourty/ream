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
    # An empty-matching pattern would flag every line of every body --
    # "fails closed" in the narrow sense (every publish refused), but only
    # ever noticed in production, by every agent, all at once.
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes("wide\t.*")


def test_duplicate_shape_names_are_rejected():
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes("dup\tfoo\ndup\tbar\n")


def test_a_shapes_pattern_using_a_whole_body_anchor_is_rejected():
    # \A/\Z anchor the whole body; scan() now searches the whole body in one
    # pass (M1), so a shape reaching for "start of line" wants ^ under
    # MULTILINE, not \A -- refuse the anchor that means something different
    # from what a per-line scan would have given.
    with pytest.raises(scan.ShapesError):
        scan.parse_shapes("bad_anchor\t\\Asecret=")


def test_a_well_formed_shapes_source_parses_without_raising():
    shapes = scan.parse_shapes("# comment\n\nsecret_assignment\tsecret=\n")
    assert [name for name, _ in shapes] == ["secret_assignment"]


# --- performance: the whole body is scanned once per shape, not once per
# line per shape. ------------------------------------------------------------


def test_a_megabyte_of_newlines_scans_in_under_two_seconds():
    # The previous line-by-line loop cost ~4 s of CPU on exactly this input
    # (22 shapes x ~1M lines, each a Python-level pattern.search call), and
    # `re` holds the GIL, so that stalled the whole process -- including its
    # own health check -- for the duration of one publish. 2 s is a generous
    # bound so this stays green on slower CI hardware, but it fails loudly
    # if that loop ever comes back.
    body = "\n" * 1_000_000
    start = time.perf_counter()
    scan.scan(body)
    assert time.perf_counter() - start < 2.0


# --- every shape gets a positive fake it must catch and a near-miss it must
# not. --------------------------------------------------------------------

SHAPE_FIXTURES: dict[str, tuple[str, str]] = {
    "authorization_header": (
        "Authorization: Bearer FAKETOKEN123",
        "Authorization required for this page",
    ),
    "bearer_token": ("Bearer FAKETOKEN123", "Bearermania is a sports talk show"),
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
    # Pins the complete set: deleting a shape, or leaving a new one
    # untested, fails this before it fails silently in production.
    assert set(SHAPE_FIXTURES) == {name for name, _ in scan.SHAPES}


@pytest.mark.parametrize("name", sorted(SHAPE_FIXTURES))
def test_shape_catches_its_fake_and_ignores_its_near_miss(name):
    positive, near_miss = SHAPE_FIXTURES[name]
    caught = scan.scan(positive)
    assert any(f.shape == name for f in caught), f"{name} did not fire on its own positive fake"
    missed = scan.scan(near_miss)
    assert not any(f.shape == name for f in missed), f"{name} fired on its near-miss"


def test_url_query_secret_stays_case_sensitive():
    # Unlike the key-name shapes, this one is reached through a URL query
    # string, where the names in practice are the lowercase ones already in
    # the source -- a capitalised key is left alone rather than widening an
    # already broad, unnamed-value-bearing pattern.
    assert scan.scan("https://example.test/page?Key=FAKETOKEN123") == []


def test_bearer_token_stays_case_sensitive():
    # Measured, not assumed: making this one shape case-insensitive alone
    # produced 38 of the ~40 net new hits across homelab's own docs/*.md,
    # because "bearer" is ordinary English/security prose in a way
    # "password="/"token="/"api_key" are not. RFC 6750's auth-scheme token
    # is `Bearer`, case-sensitive, anyway.
    assert scan.scan("the bearer of this token must present it") == []
    assert scan.scan("Bearer FAKETOKEN123") != []


# --- the two BRE-translated shapes: proof the translation was necessary,
# not just claimed. -----------------------------------------------------


def test_pasting_either_untranslated_bre_source_back_in_would_go_undetected(monkeypatch):
    # Guards against exactly the mistake the shapes file's header warns
    # about: pasting the homelab source's BRE line back in verbatim compiles
    # without error under Python's `re` (it is not invalid syntax, just the
    # wrong syntax) and then never matches a real credential again --
    # silently, with no test failure, unless this one exists.
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


# --- placeholder values are excluded, real-looking ones are not ------------


@pytest.mark.parametrize(
    "body",
    [
        "password=<YOUR_PASSWORD_HERE>",
        "token=${GITHUB_TOKEN}",
        "secret={{ vault_secret }}",
        "password=********",
        "password=xxxxxxxx",
        "password=changeme",
        "api_key=example",
        "token=REDACTED",
    ],
)
def test_placeholder_values_are_excluded(body):
    assert scan.scan(body) == []


def test_an_assignment_with_no_value_at_all_is_treated_as_a_placeholder():
    assert scan.scan("password=") == []
    assert scan.scan("password=\n") == []


def test_a_realistic_looking_value_is_not_mistaken_for_a_placeholder():
    assert scan.scan("password=hunter2fake123") != []


def test_a_placeholder_body_password_wrapped_in_a_url_still_flags_the_url_shape():
    # Placeholder exclusion is per-match, not per-body: a value that looks
    # like a placeholder to one shape does not suppress a different shape
    # firing on genuinely non-placeholder content elsewhere in the same body.
    findings = scan.scan("password=<REDACTED>\ntoken=FAKETOKEN123")
    assert any(f.shape == "token_assignment" for f in findings)
    assert not any(f.shape == "password_assignment" for f in findings)


def test_a_bare_secret_with_nothing_after_it_still_fires():
    # aws_access_key_id and the header shapes are NOT assignment shapes: the
    # match itself is the whole secret (or a header announcing one), so
    # "nothing after it" must not be read as a placeholder -- unlike
    # `password=` with nothing after it, which is exactly what "empty"
    # placeholder detection is for.
    assert scan.scan(FAKE_AWS) != []
    assert scan.scan(FAKE_PRIVATE_KEY) != []
