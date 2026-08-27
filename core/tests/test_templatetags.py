import pytest
from django import template
from django.template import Context, Template


def render(snippet, **context):
    tpl = Template("{% load icons %}" + snippet)
    return tpl.render(Context(context))


def test_icon_renders_svg_wrapper_and_known_geometry():
    output = render('{% icon "x" %}')
    assert output.startswith("<svg")
    assert output.endswith("</svg>")
    assert 'class="h-4 w-4"' in output
    assert 'stroke-width="1.75"' in output
    assert 'viewBox="0 0 24 24"' in output
    assert 'aria-hidden="true"' in output
    assert '<line x1="18" y1="6" x2="6" y2="18"/>' in output
    assert '<line x1="6" y1="6" x2="18" y2="18"/>' in output


def test_icon_renders_pencil_geometry():
    output = render('{% icon "pencil" %}')
    assert '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987' in output
    assert '<path d="m15 5 4 4" />' in output


def test_icon_css_class_override_is_applied():
    output = render('{% icon "x" css_class="h-6 w-6" %}')
    assert 'class="h-6 w-6"' in output


def test_icon_stroke_width_override_is_applied():
    output = render('{% icon "x" stroke_width="2" %}')
    assert 'stroke-width="2"' in output


def test_icon_unknown_name_raises_template_syntax_error():
    with pytest.raises(template.TemplateSyntaxError, match="Unknown icon 'nope'"):
        render('{% icon "nope" %}')


def render_brand(snippet, **context):
    tpl = Template("{% load brand %}" + snippet)
    return tpl.render(Context(context))


def test_brand_renders_bold_accent_wordmark():
    output = render_brand("{% brand %}")
    assert output == '<span class="font-bold text-accent">very lift</span>'


def test_brand_as_variable_is_safe_inside_blocktrans():
    # blocktrans HTML-escapes any substituted variable unless it's marked
    # safe -- this is the regression that matters: forgetting mark_safe in
    # brand() would silently turn the <span> into escaped text here.
    output = render_brand(
        "{% load i18n %}{% brand as brand_name %}"
        "{% blocktrans %}Hello {{ brand_name }}.{% endblocktrans %}"
    )
    assert output == 'Hello <span class="font-bold text-accent">very lift</span>.'
