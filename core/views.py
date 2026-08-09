"""Cross-cutting views that belong to no single feature app.

Currently just the authenticated gate in front of MEDIA_ROOT (TASK-277).
"""

import logging

from django.core.exceptions import PermissionDenied
from django.views.static import serve as serve_static

logger = logging.getLogger(__name__)


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
