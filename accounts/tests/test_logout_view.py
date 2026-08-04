"""Integration tests for OIDC logout through mozilla_django_oidc's oidc_logout view."""

import pytest
from django.shortcuts import resolve_url
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory


@pytest.fixture
def oidc_settings(settings):
    settings.OIDC_OP_LOGOUT_ENDPOINT = (
        "https://auth.example.com/application/o/verylift/end-session/"
    )
    settings.OIDC_RP_CLIENT_ID = "test-client-id"
    return settings


@pytest.mark.django_db
class TestOIDCLogoutView:
    def _login_oidc_user(self, client, oidc_sub, id_token=None):
        client.force_login(UserFactory(oidc_sub=oidc_sub))
        if id_token is not None:
            session = client.session
            session["oidc_id_token"] = id_token
            session.save()

    def test_oidc_user_logout_redirects_to_provider_end_session_endpoint(
        self, oidc_settings
    ):
        client = Client()
        self._login_oidc_user(client, "sub-1", id_token="tok-123")

        response = client.post(reverse("oidc_logout"))

        assert response.status_code == 302
        assert response["Location"].startswith(oidc_settings.OIDC_OP_LOGOUT_ENDPOINT)
        assert "id_token_hint=tok-123" in response["Location"]

    def test_oidc_user_logout_ends_local_session_too(self, oidc_settings):
        client = Client()
        self._login_oidc_user(client, "sub-1", id_token="tok-123")

        client.post(reverse("oidc_logout"))

        assert "_auth_user_id" not in client.session

    def test_local_only_user_logout_is_unaffected(self, oidc_settings):
        client = Client()
        client.force_login(UserFactory())

        response = client.post(reverse("oidc_logout"))

        assert response.status_code == 302
        assert response["Location"] == resolve_url(oidc_settings.LOGOUT_REDIRECT_URL)
        assert oidc_settings.OIDC_OP_LOGOUT_ENDPOINT not in response["Location"]

    def test_oidc_user_missing_id_token_falls_back_to_local_logout(self, oidc_settings):
        client = Client()
        self._login_oidc_user(client, "sub-2", id_token=None)

        response = client.post(reverse("oidc_logout"))

        assert response.status_code == 302
        assert response["Location"] == resolve_url(oidc_settings.LOGOUT_REDIRECT_URL)
        assert oidc_settings.OIDC_OP_LOGOUT_ENDPOINT not in response["Location"]

    def test_sidebar_renders_post_form_for_oidc_logout(self, oidc_settings):
        client = Client()
        client.force_login(UserFactory(oidc_sub="sub-3"))

        response = client.get(reverse("accounts:settings"))

        content = response.content.decode()
        expected_action = reverse("oidc_logout")
        assert f'<form method="post" action="{expected_action}">' in content
        assert f'<a href="{expected_action}"' not in content
