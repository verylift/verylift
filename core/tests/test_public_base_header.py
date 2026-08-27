"""Regression coverage for the `{% block header %}` seam added to
templates/base/public.html so landing.html can override the header layout
without touching it for every other page that extends public.html
(challenges/invite_link_preview.html, base/onboarding.html, etc.)."""

from django.template import Context, Template


def test_public_base_default_header_is_unchanged_when_not_overridden():
    tpl = Template(
        '{% extends "base/public.html" %}{% block page_body %}{% endblock %}'
    )
    output = tpl.render(Context({}))
    assert (
        '<header class="py-6 px-4 flex items-center justify-between max-w-4xl '
        'mx-auto w-full">' in output
    )
    assert '<span class="text-accent font-bold text-2xl">very lift</span>' in output


def test_public_base_header_block_is_overridable():
    tpl = Template(
        '{% extends "base/public.html" %}'
        '{% block header %}<div id="custom-header"></div>{% endblock %}'
        "{% block page_body %}{% endblock %}"
    )
    output = tpl.render(Context({}))
    assert '<div id="custom-header"></div>' in output
    assert "justify-between" not in output
