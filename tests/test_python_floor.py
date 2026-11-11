"""The parser bounds rely on the standard library's patched `html.parser` (CPython 3.12.15
and later): an older interpreter reads a tag another way, so the guarantees of
`ream.htmlbound` are not the ones its tests were written against."""

import sys

import pytest

FLOOR = (3, 12, 15)


def test_the_interpreter_has_the_patched_html_parser():
    assert sys.version_info >= FLOOR, (
        f"Python {'.'.join(map(str, sys.version_info[:3]))} is older than "
        f"{'.'.join(map(str, FLOOR))}: its html.parser is not the one the parser "
        "bounds in ream.htmlbound rely on (the image and CI must use a newer one)"
    )


@pytest.mark.parametrize("version", [(3, 12, 14), (3, 11, 9), (3, 9, 99)])
def test_the_floor_refuses_an_older_interpreter(version):
    assert version < FLOOR
