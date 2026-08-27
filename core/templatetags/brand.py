"""Custom rendering for the "very lift" wordmark in running copy.

Sibling to core/templatetags/icons.py -- a fixed-content simple_tag rather
than a filter, since there's no input text to transform: the wordmark is a
single constant string, always styled the same way.

Scope: for the wordmark's appearance inside body copy/marketing prose only.
Do NOT use this in <title>/meta tags, aria-label attributes, plain-text
email templates, or legal/policy documents -- those either can't render
markup at all, or the styling reads out of register in a defined legal term.
"""

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def brand():
    """Render "very lift" in bold, brand-accent green.

    Language-invariant: the wordmark reads the same in every locale, so this
    never routes through gettext.

    {% blocktrans %} can't contain block tags directly, so inside one, use
    {% brand as brand_name %} first and reference {{ brand_name }}.
    """
    return mark_safe('<span class="font-bold text-accent">very lift</span>')
