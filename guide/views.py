"""Views for the in-app user guide (renders docs/*.md as HTML pages)."""

import logging
import re

import markdown
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _t

logger = logging.getLogger(__name__)

# slug -> (title, filename). Titles mirror each file's top-level `# H1` heading.
DOC_PAGES = {
    "index": (_t("Welcome to very lift"), "index.md"),
    "scoring": (_t("How scoring works"), "scoring.md"),
    "challenges": (_t("Running a challenge"), "challenges.md"),
    "sync-and-data-freshness": (
        _t("Keeping your data fresh"),
        "sync-and-data-freshness.md",
    ),
    "bodyweight-and-units": (
        _t("Units & added-weight lifts"),
        "bodyweight-and-units.md",
    ),
}

# filename -> slug, used to rewrite the docs' own relative cross-links
# (e.g. "scoring.md", written for GitHub browsing) into in-app guide URLs.
_FILENAME_TO_SLUG = {filename: slug for slug, (_, filename) in DOC_PAGES.items()}
_MD_LINK_RE = re.compile(r'href="([\w.-]+\.md)"')


def _rewrite_doc_links(html):
    """Rewrite href="<file>.md" links into the corresponding guide:page URL."""

    def _replace(match):
        filename = match.group(1)
        slug = _FILENAME_TO_SLUG.get(filename)
        if slug is None:
            return match.group(0)
        url = (
            reverse("guide:index")
            if slug == "index"
            else reverse("guide:page", kwargs={"slug": slug})
        )
        return f'href="{url}"'

    return _MD_LINK_RE.sub(_replace, html)


def _render_doc(request, slug):
    """Render the docs/<filename>.md for the given slug into the shared template."""
    title, filename = DOC_PAGES[slug]
    text = (settings.BASE_DIR / "docs" / filename).read_text()
    rendered = markdown.markdown(text, extensions=["fenced_code", "tables"])
    rendered = _rewrite_doc_links(rendered)
    # Content is authored by the dev team as part of the repo, not user input, so
    # marking it safe here does not introduce an XSS risk.
    content_html = mark_safe(rendered)
    return render(
        request,
        "docs/page.html",
        {
            "title": title,
            "content_html": content_html,
            "nav_pages": DOC_PAGES.items(),
            "active_slug": slug,
        },
    )


@login_required
def index_view(request):
    """Render the guide's landing page (docs/index.md)."""
    return _render_doc(request, "index")


@login_required
def page_view(request, slug):
    """Render a single guide page identified by slug, or 404 if unknown."""
    if slug not in DOC_PAGES:
        logger.warning("Unknown user guide slug requested: %s", slug)
        raise Http404()
    return _render_doc(request, slug)
