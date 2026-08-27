"""Custom error page rendering (TASK-186).

Verifies that 400/403/404/500 responses render the app-styled templates under a
prod-like DEBUG=False run, that CSRF failures route through the styled 403, and
that DEBUG=True local behaviour (Django's technical debug pages) is unchanged.
"""

import pytest
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.test.utils import override_settings

from accounts.tests.factories import UserFactory


def _template_names(response):
    return [t.name for t in response.templates if t.name]


def _logged_in_client(raise_request_exception=True):
    user = UserFactory()
    client = Client(raise_request_exception=raise_request_exception)
    client.force_login(user)
    return client


@pytest.mark.django_db
@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
def test_404_renders_styled_template():
    response = _logged_in_client().get("/docs/no-such-page/")

    assert response.status_code == 404
    assert "404.html" in _template_names(response)
    content = response.content.decode()
    assert "404" in content
    assert "Traceback" not in content


@pytest.mark.django_db
@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
def test_403_renders_styled_template_for_permission_denied(monkeypatch):
    def _boom(request, slug):
        raise PermissionDenied

    monkeypatch.setattr("guide.views._render_doc", _boom)

    response = _logged_in_client(raise_request_exception=False).get("/docs/index/")

    assert response.status_code == 403
    assert "403.html" in _template_names(response)
    content = response.content.decode()
    assert "403" in content


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
def test_403_renders_styled_template_for_csrf_failure():
    response = Client(enforce_csrf_checks=True).post("/accounts/login/", {})

    assert response.status_code == 403
    assert "403.html" in _template_names(response)


@pytest.mark.django_db
@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
def test_500_renders_styled_template(monkeypatch):
    def _boom(request, slug):
        raise Exception("boom")

    monkeypatch.setattr("guide.views._render_doc", _boom)

    response = _logged_in_client(raise_request_exception=False).get("/docs/index/")

    assert response.status_code == 500
    assert "500.html" in _template_names(response)
    content = response.content.decode()
    assert "Traceback" not in content


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_debug_true_shows_technical_page():
    response = _logged_in_client(raise_request_exception=False).get(
        "/docs/no-such-page/"
    )

    assert response.status_code == 404
    assert "404.html" not in _template_names(response)
