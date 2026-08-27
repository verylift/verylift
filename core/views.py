"""Cross-cutting views that belong to no single feature app."""

import logging

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.static import serve as serve_static
from django_ratelimit.decorators import ratelimit

from accounts.ratelimit import client_ip
from core.forms import NewsletterSubscribeForm
from core.models import NewsletterSubscriber, SiteSettings

logger = logging.getLogger(__name__)


def _newsletter_ip_rate(group, request):
    return settings.RATELIMIT_NEWSLETTER_IP


def landing_view(request):
    """Public landing page at ``/``.

    Anonymous visitors get the marketing page (``landing.html``); already
    authenticated visitors are sent straight to their dashboard.
    """
    if request.user.is_authenticated:
        return redirect("challenges:dashboard")
    context = {"discord_invite_url": SiteSettings.load().discord_invite_url}
    return render(request, "landing.html", context)


@ratelimit(
    group="newsletter_ip", key=client_ip, rate=_newsletter_ip_rate, method="POST"
)
def newsletter_subscribe_view(request):
    """Handles the "Get the newsletter" form on the landing page.

    A duplicate email is treated as an idempotent success rather than an
    error — a returning subscriber shouldn't see a validation failure.
    """
    form = NewsletterSubscribeForm(request.POST)
    if form.is_valid():
        email = form.cleaned_data["email"]
        _, created = NewsletterSubscriber.objects.get_or_create(email=email)
        if created:
            logger.info("New newsletter subscription")
        messages.success(request, gettext("You're subscribed. Thanks for joining!"))
        return redirect(f"{reverse('core:landing')}#newsletter")

    for error in form.errors.get("email", []):
        messages.error(request, error)
    return redirect(f"{reverse('core:landing')}#newsletter")


def supported_apps_view(request):
    """Lists every workout-tracking app "very easy" links to (TASK-254).

    Hardcoded content, not model-backed: a tracker can only appear here
    once its client/importer already shipped, so the row can never lead
    the deploy it describes -- and a DB-sourced description would sit
    outside the gettext catalog, so the Spanish page would silently
    render English while everything else translated (verified: the seed
    descriptions never appeared in locale/es/LC_MESSAGES/django.po).
    """
    return render(request, "supported_apps.html")


def protected_media_view(request, path, document_root=None):
    """Serve a MEDIA_ROOT file, but only to an authenticated session.

    Django itself stays the sole media server here: the same
    ``django.views.static.serve`` still streams the bytes (keeping its
    ``Last-Modified``/conditional-GET handling, so repeat views remain cheap),
    and MEDIA_URL/MEDIA_ROOT are unchanged — see the MEDIA_ROOT comment in
    ``root/settings.py``, whose "small self-hosted app at low scale"
    premise this preserves. The usual alternative, handing the file off to a
    reverse proxy via ``X-Accel-Redirect``, has nothing to hand off *to* in this
    deployment: ``docker-compose.selfhost.yml`` runs gunicorn directly with the
    media volume inside the app container, and the external tunnel client in
    front of it cannot read that volume. All this adds is an ``is_authenticated``
    check that ``AuthenticationMiddleware`` has already computed.

    The gate is deliberately the whole ``/media/`` prefix rather than
    ``avatars/`` (the only ``upload_to`` today), so a future upload field is
    covered by default instead of silently shipping un-gated. The check runs
    before any filesystem access, so an anonymous request gets the same 403
    whether or not the path exists and nothing about file existence leaks.

    This is a binary authenticated/anonymous gate only. Full friends-only and
    blocked-user-aware *per-viewer* avatar gating is intentionally out of scope:
    it depends on the still-unimplemented friend/block model (TASK-179 scoping;
    TASK-187 data model, TASK-189 blocking, TASK-191 per-viewer roster and
    leaderboard filtering; TASK-190 is cancelled). Even once the "Private
    Participant" masking that scoping introduces hides an identity in rendered
    HTML, the avatar file will still need its own equivalent per-viewer check —
    that follow-on belongs with TASK-191, not here.
    """
    if not request.user.is_authenticated:
        logger.warning("Rejected anonymous request for media path %s", path)
        raise PermissionDenied

    response = serve_static(request, path, document_root=document_root)
    # Keep the file out of *shared* caches. This deployment sits behind a CDN
    # that caches by URL alone, ignoring cookies, for image extensions
    # (.avif included), so without this an authenticated fetch would populate
    # an edge entry that a later anonymous request could be served from —
    # never reaching the check above. `private` still lets each user's own
    # browser cache their own view (and keeps serve_static's conditional-GET
    # handling useful), which `no-store` would not. Nothing here adds
    # `Vary: Cookie` on top: it is redundant with `private` for shared caches,
    # and asking for it would only bust the browser's own cache on every
    # session-cookie rotation for no security gain. (Django's session and
    # locale middleware set their own Vary headers; those are left alone.)
    response["Cache-Control"] = "private"
    return response
