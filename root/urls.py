from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path
from django.views.generic import TemplateView

from core.views import protected_media_view

urlpatterns = [
    # Vanity path (env-configurable, see settings.ADMIN_URL_PATH) instead of
    # Django's default admin path, which is the first thing every scanner/bot
    # probes (TASK-284).
    path(settings.ADMIN_URL_PATH, admin.site.urls),
    # Served by Django behind an authentication check (TASK-277), not via
    # WhiteNoise — WhiteNoise only covers STATIC_ROOT, which is why a
    # view-level gate is sufficient here. Fine at this app's scale; see the
    # MEDIA_ROOT comment in settings.py.
    path(
        "media/<path:path>",
        protected_media_view,
        {"document_root": settings.MEDIA_ROOT},
        name="media",
    ),
    path("i18n/", include("django.conf.urls.i18n")),
    path("oidc/", include("mozilla_django_oidc.urls")),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    # Static legal pages, anonymously readable (linked from the registration flow).
    path(
        "terms/",
        TemplateView.as_view(template_name="legal/terms.html"),
        name="terms",
    ),
    path(
        "privacy/",
        TemplateView.as_view(template_name="legal/privacy.html"),
        name="privacy",
    ),
    path(
        "robots.txt",
        TemplateView.as_view(template_name="robots.txt", content_type="text/plain"),
        name="robots-txt",
    ),
    path(
        "llms.txt",
        TemplateView.as_view(template_name="llms.txt", content_type="text/plain"),
        name="llms-txt",
    ),
    path("", include("core.urls")),
    path("", include("accounts.urls")),
    path("", include("challenges.urls")),
    path("", include("notifications.urls")),
    path("", include("guide.urls")),
    path("policies/", include("policies.urls")),
]
