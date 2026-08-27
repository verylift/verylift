"""URL configuration for the core app."""

from django.urls import path

from core import views

app_name = "core"


urlpatterns = [
    path("", views.landing_view, name="landing"),
    path(
        "newsletter/subscribe/",
        views.newsletter_subscribe_view,
        name="newsletter-subscribe",
    ),
    path(
        "supported-apps/",
        views.supported_apps_view,
        name="supported-apps",
    ),
]
