"""Template context processors for the core app."""

from django.conf import settings


def app_version(request):
    """Expose the running app version (see root.settings.APP_VERSION)."""
    return {"app_version": settings.APP_VERSION}


def discord_invite_url(request):
    """Expose the Discord invite link (see root.settings.DISCORD_INVITE_URL)."""
    return {"discord_invite_url": settings.DISCORD_INVITE_URL}
