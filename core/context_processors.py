"""Template context processors for the core app."""

from django.conf import settings

from core.models import SiteSettings


def app_version(request):
    """Expose the running app version (see root.settings.APP_VERSION)."""
    return {"app_version": settings.APP_VERSION}


def discord_invite_url(request):
    """Expose the admin-configurable Discord invite link (core.models.SiteSettings)."""
    return {"discord_invite_url": SiteSettings.load().discord_invite_url}
