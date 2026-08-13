import pytest
from django.urls import reverse

from accounts.tests.factories import UserFactory
from policies.tests.factories import PolicyConsentFactory, PolicyVersionFactory

pytestmark = pytest.mark.django_db


class TestPolicyConsentMiddleware:
    def test_redirects_authenticated_user_with_pending_gated_consent(self, client):
        user = UserFactory()
        PolicyVersionFactory(is_active=True)
        client.force_login(user)

        response = client.get(reverse("challenges:dashboard"))

        assert response.status_code == 302
        assert response.url.startswith(reverse("policies:consent"))

    def test_allows_authenticated_user_who_has_consented(self, client):
        user = UserFactory()
        version = PolicyVersionFactory(is_active=True)
        PolicyConsentFactory(user=user, policy_version=version)
        client.force_login(user)

        response = client.get(reverse("challenges:dashboard"))

        assert response.status_code == 200

    def test_allows_authenticated_user_when_no_gated_version_exists(self, client):
        user = UserFactory()
        client.force_login(user)

        response = client.get(reverse("challenges:dashboard"))

        assert response.status_code == 200

    def test_does_not_redirect_an_anonymous_request(self, client):
        PolicyVersionFactory(is_active=True)

        response = client.get(reverse("core:landing"))

        assert response.status_code == 200

    def test_consent_page_itself_is_exempt(self, client):
        user = UserFactory()
        PolicyVersionFactory(is_active=True)
        client.force_login(user)

        response = client.get(reverse("policies:consent"))

        assert response.status_code == 200

    def test_a_version_that_does_not_gate_access_does_not_redirect(self, client):
        user = UserFactory()
        PolicyVersionFactory(is_active=True, policy__gates_access=False)
        client.force_login(user)

        response = client.get(reverse("challenges:dashboard"))

        assert response.status_code == 200
