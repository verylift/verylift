"""Tests for LocalLoginView and local-auth related code."""

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory

User = get_user_model()


@pytest.mark.django_db
class TestLocalLoginView:
    def test_get_returns_200(self):
        response = Client().get(reverse("accounts:login"))
        assert response.status_code == 200

    def test_get_uses_login_template(self):
        response = Client().get(reverse("accounts:login"))
        assert "registration/login.html" in [t.name for t in response.templates]

    def test_oidc_configured_false_when_client_id_empty(self, settings):
        settings.OIDC_RP_CLIENT_ID = ""
        response = Client().get(reverse("accounts:login"))
        assert response.context["oidc_configured"] is False

    def test_oidc_configured_true_when_client_id_set(self, settings):
        settings.OIDC_RP_CLIENT_ID = "some-client-id"
        response = Client().get(reverse("accounts:login"))
        assert response.context["oidc_configured"] is True

    def test_login_page_renders_provider_name(self, settings):
        settings.OIDC_RP_CLIENT_ID = "some-client-id"
        settings.OIDC_PROVIDER_NAME = "Keycloak"
        response = Client().get(reverse("accounts:login"))
        assert response.context["oidc_provider_name"] == "Keycloak"
        assert b"Sign in with Keycloak" in response.content

    def test_valid_credentials_redirects(self, db):
        user = UserFactory(username="localdev")
        user.set_password("secret")
        user.save()
        response = Client().post(
            reverse("accounts:login"),
            {"username": "localdev", "password": "secret"},
        )
        assert response.status_code == 302

    def test_login_page_links_to_password_reset(self):
        response = Client().get(reverse("accounts:login"))
        assert reverse("accounts:password-reset").encode() in response.content

    def test_invalid_credentials_returns_200(self, db):
        response = Client().post(
            reverse("accounts:login"),
            {"username": "nobody", "password": "wrong"},
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestOIDCOnlyLoginMode:
    """OIDC-only login mode (TASK-233): no local form, no local POST path."""

    @pytest.fixture
    def oidc_only(self, settings):
        settings.OIDC_ONLY_LOGIN = True
        settings.OIDC_RP_CLIENT_ID = "client-id"
        settings.OIDC_RP_CLIENT_SECRET = "client-secret"
        settings.OIDC_OP_AUTHORIZATION_ENDPOINT = "https://idp.example/authorize"
        settings.OIDC_OP_TOKEN_ENDPOINT = "https://idp.example/token"
        settings.OIDC_OP_USER_ENDPOINT = "https://idp.example/userinfo"
        settings.OIDC_OP_JWKS_ENDPOINT = "https://idp.example/jwks"
        return settings

    def test_get_redirects_to_oidc_init(self, oidc_only):
        response = Client().get(reverse("accounts:login"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("oidc_authentication_init"))

    def test_get_never_renders_local_login_template(self, oidc_only):
        response = Client().get(reverse("accounts:login"))
        assert "registration/login.html" not in [t.name for t in response.templates]

    def test_next_param_is_carried_into_the_redirect(self, oidc_only):
        response = Client().get(reverse("accounts:login"), {"next": "/settings/"})
        assert response.status_code == 302
        assert response["Location"] == (
            f"{reverse('oidc_authentication_init')}?next=%2Fsettings%2F"
        )

    def test_post_with_valid_credentials_does_not_log_in_locally(self, oidc_only):
        user = UserFactory(username="localdev")
        user.set_password("secret")
        user.save()
        client = Client()

        response = client.post(
            reverse("accounts:login"),
            {"username": "localdev", "password": "secret"},
        )

        assert response.status_code == 302
        assert response["Location"].startswith(reverse("oidc_authentication_init"))
        assert "_auth_user_id" not in client.session

    def test_post_next_param_is_carried_into_the_redirect(self, oidc_only):
        response = Client().post(
            reverse("accounts:login"),
            {"username": "localdev", "password": "secret", "next": "/settings/"},
        )
        assert response["Location"] == (
            f"{reverse('oidc_authentication_init')}?next=%2Fsettings%2F"
        )

    def test_flag_off_leaves_local_login_untouched(self, settings):
        settings.OIDC_ONLY_LOGIN = False
        response = Client().get(reverse("accounts:login"))
        assert response.status_code == 200
        assert "registration/login.html" in [t.name for t in response.templates]


@pytest.mark.django_db
class TestCreateUserWithPassword:
    def test_create_user_with_password_has_usable_password(self):
        user = User.objects.create_user(username="pwuser", password="hunter2")
        assert user.has_usable_password()

    def test_create_user_without_password_has_unusable_password(self):
        user = User.objects.create_user(username="nopwuser")
        assert not user.has_usable_password()


@pytest.mark.django_db
class TestCreateLocalUserCommand:
    def test_creates_new_user(self, capsys):
        call_command("create_local_user", username="cliuser", password="pass")
        user = User.objects.get(username="cliuser")
        assert user.has_usable_password()
        assert "Created" in capsys.readouterr().out

    def test_updates_existing_user(self, capsys):
        UserFactory(username="existing")
        call_command("create_local_user", username="existing", password="newpass")
        user = User.objects.get(username="existing")
        assert user.has_usable_password()
        assert "Updated" in capsys.readouterr().out

    def test_sets_display_name(self):
        call_command(
            "create_local_user",
            username="named",
            password="pass",
            display_name="Dev User",
        )
        user = User.objects.get(username="named")
        assert user.display_name == "Dev User"
