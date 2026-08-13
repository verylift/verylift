"""Tests for core admin customizations."""

import pytest

from core.admin import _csv_safe


@pytest.mark.parametrize(
    "value",
    ["=cmd|calc", "+1-234-5555", "-2+3", "@evil", "\ttabbed", "\rcarriage"],
)
def test_csv_safe_neutralizes_formula_prefixes(value):
    """A cell starting with one of these is a formula to Excel/Sheets, not
    text -- Django's EmailField accepts e.g. "-2+3@example.com" as a valid
    address, so this can happen with a real, unmodified subscriber email."""
    assert _csv_safe(value) == f"'{value}"


def test_csv_safe_leaves_ordinary_email_untouched():
    assert _csv_safe("person@example.com") == "person@example.com"
