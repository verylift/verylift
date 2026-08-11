from django.test import RequestFactory

from core.context_processors import app_version


def test_app_version_returns_configured_setting(settings):
    settings.APP_VERSION = "2026.8.4"
    context = app_version(RequestFactory().get("/"))
    assert context == {"app_version": "2026.8.4"}


def test_app_version_defaults_to_dev(settings):
    settings.APP_VERSION = "dev"
    context = app_version(RequestFactory().get("/"))
    assert context == {"app_version": "dev"}
