"""Tests for the extracted components/_copy_code.html partial.

Split out of _liftosaur_coupon_cta.html (TASK-254 follow-up) so a page that
only wants a bare code doesn't duplicate the clipboard JS. These render the
partial directly rather than through any specific page, since it has no
view of its own.
"""

from django.template import Context, Template


def render(code):
    tpl = Template(
        '{% include "components/_copy_code.html" with code="' + code + '" %}'
    )
    return tpl.render(Context({}))


def test_renders_the_given_code_not_a_hardcoded_one():
    assert "ABC123" in render("ABC123")
    assert "VERYLIFT" not in render("ABC123")


def test_scoped_by_data_attribute_not_a_page_unique_id():
    output = render("VERYLIFT")
    assert "data-copy-code" in output
    assert 'id="liftosaur-coupon-cta"' not in output
    assert "id=" not in output


def test_two_instances_on_one_page_do_not_share_an_id():
    output = render("FIRST") + render("SECOND")
    assert output.count("data-copy-code>") == 2
    assert "FIRST" in output and "SECOND" in output


def test_ships_no_inline_script():
    """TASK-341: the handler is one delegated listener in base/base.html, so
    the partial must stay pure markup. Re-adding an inline <script> here
    brings back both the duplicated payload and the currentScript scoping
    hack that a second chip on the page needed.
    """
    assert "<script" not in render("VERYLIFT")


def test_button_exposes_the_hooks_the_delegated_handler_keys_off():
    """The handler lives in a different file, so these attribute names are a
    cross-file contract: renaming one side silently breaks copying with
    nothing else to catch it.
    """
    output = render("VERYLIFT")

    assert 'data-copy-text="VERYLIFT"' in output
    for hook in (
        "data-copy-label",
        "data-copied-label",
        "data-copy-icon",
        "data-copied-icon",
    ):
        assert hook in output
