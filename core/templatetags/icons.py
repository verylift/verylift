"""Inline Lucide icons (ISC license, https://lucide.dev) for server-rendered templates.

This app has no JS icon runtime — icons are inlined at render time. Adding a
new icon means adding its inner markup here once, not copy-pasting an <svg>
block into every template that needs it.
"""

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

# Inner markup only (no outer <svg>) — verbatim from the templates being
# converted, geometry unmodified. Styling (stroke color/width, size) is
# applied by the wrapper below so every icon shares one visual spec.
_ICONS = {
    "pencil": (
        '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83'
        'l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z" />'
        '<path d="m15 5 4 4" />'
    ),
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "infinity": (
        '<path d="M18.178 8c5.096 0 5.096 8 0 8-5.095 0-7.133-8-12.739-8'
        '-4.585 0-4.585 8 0 8 5.606 0 7.644-8 12.74-8Z"/>'
    ),
    "menu": (
        '<line x1="4" y1="6" x2="20" y2="6"/>'
        '<line x1="4" y1="12" x2="20" y2="12"/>'
        '<line x1="4" y1="18" x2="20" y2="18"/>'
    ),
    "chevron-left": '<polyline points="15 18 9 12 15 6"></polyline>',
    "chevron-right": '<polyline points="9 18 15 12 9 6"></polyline>',
    "plus": (
        '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'
    ),
    "log-out": (
        '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
        '<polyline points="16 17 21 12 16 7"/>'
        '<line x1="21" y1="12" x2="9" y2="12"/>'
    ),
    "trash-2": (
        '<path d="M3 6h18"/>'
        '<path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>'
        '<path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>'
        '<line x1="10" y1="11" x2="10" y2="17"/>'
        '<line x1="14" y1="11" x2="14" y2="17"/>'
    ),
    "layout-dashboard": (
        '<rect width="7" height="9" x="3" y="3" rx="1" />'
        '<rect width="7" height="5" x="14" y="3" rx="1" />'
        '<rect width="7" height="9" x="14" y="12" rx="1" />'
        '<rect width="7" height="5" x="3" y="16" rx="1" />'
    ),
    "copy": (
        '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/>'
        '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>'
    ),
    "trophy": (
        '<path d="M10 14.66V17a1 1 0 0 1-1 1 2 2 0 0 0-2 2v2" />'
        '<path d="M14 14.66V17a1 1 0 0 0 1 1 2 2 0 0 1 2 2v2" />'
        '<path d="M17.916 10H19.5A2.5 2.5 0 0 0 22 7.5V5a1 1 0 0 0-1-1h-3" />'
        '<path d="M4 22h16" />'
        '<path d="M6 9a6 6 0 0 0 12 0V3a1 1 0 0 0-1-1H7a1 1 0 0 0-1 1z" />'
        '<path d="M6.084 10H4.5A2.5 2.5 0 0 1 2 7.5V5a1 1 0 0 1 1-1h3" />'
    ),
}


@register.simple_tag
def icon(name, css_class="h-4 w-4", stroke_width="1.75"):
    """Render an inline Lucide icon. Usage: {% icon "pencil" css_class="h-5 w-5" %}"""
    inner = _ICONS.get(name)
    if inner is None:
        raise template.TemplateSyntaxError(
            f"Unknown icon {name!r}. Add its geometry to core/templatetags/icons.py."
        )
    safe_class = escape(css_class)
    safe_stroke_width = escape(stroke_width)
    return mark_safe(
        f'<svg class="{safe_class}" fill="none" stroke="currentColor" '
        f'stroke-width="{safe_stroke_width}" stroke-linecap="round" '
        f'stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true">{inner}</svg>'
    )
