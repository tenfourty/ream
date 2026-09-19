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
    for finding in findings:
        assert FAKE_AWS not in repr(finding)


def test_a_shapes_line_missing_its_tab_fails_closed_instead_of_matching_everything():
    # A line with no separator must not silently become a shape whose pattern
    # is the empty string -- an empty regex matches every line, so one
    # malformed source line would turn into a scanner that flags everything
    # it is given, forever. Refusing to load is the safe failure.
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


def test_a_well_formed_shapes_source_parses_without_raising():
    shapes = scan.parse_shapes("# comment\n\nsecret_assignment\tsecret=\n")
    assert [name for name, _ in shapes] == ["secret_assignment"]
