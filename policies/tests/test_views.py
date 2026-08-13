import pytest
from django.urls import reverse

from accounts.tests.factories import UserFactory
from policies.models import PolicyConsent
from policies.tests.factories import PolicyFactory, PolicyVersionFactory

pytestmark = pytest.mark.django_db


class TestPolicyListView:
    def test_lists_policies_to_anonymous_visitors(self, client):
        policy = PolicyFactory(name="Terms of Service")

        response = client.get(reverse("policies:list"))

        assert response.status_code == 200
        assert policy in response.context["policies"]


class TestPolicyDetailView:
    def test_shows_the_active_version(self, client):
        policy = PolicyFactory()
        version = PolicyVersionFactory(policy=policy, version="2.0", is_active=True)

        response = client.get(reverse("policies:detail", args=[policy.slug]))

        assert response.status_code == 200
        assert response.context["active_version"] == version

    def test_a_policy_with_no_versions_has_no_active_version(self, client):
        policy = PolicyFactory()

        response = client.get(reverse("policies:detail", args=[policy.slug]))

        assert response.status_code == 200
        assert response.context["active_version"] is None

    def test_unknown_slug_is_a_404(self, client):
        response = client.get(reverse("policies:detail", args=["does-not-exist"]))

        assert response.status_code == 404


class TestConsentView:
    def test_anonymous_visitor_is_redirected_to_login(self, client):
        response = client.get(reverse("policies:consent"))

        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_shows_pending_gated_versions(self, client):
        user = UserFactory()
        client.force_login(user)
        version = PolicyVersionFactory(is_active=True)

        response = client.get(reverse("policies:consent"))

        assert response.status_code == 200
        assert version in response.context["pending_versions"]

    def test_redirects_away_when_nothing_is_pending(self, client):
        user = UserFactory()
        client.force_login(user)

        response = client.get(reverse("policies:consent"))

        assert response.status_code == 302

    def test_posting_agreement_records_consent_for_every_pending_version(self, client):
        user = UserFactory()
        client.force_login(user)
        version = PolicyVersionFactory(is_active=True)

        response = client.post(reverse("policies:consent"), {"agreed": "on"})

        assert response.status_code == 302
        consent = PolicyConsent.objects.get(user=user, policy_version=version)
        assert consent.method == PolicyConsent.Method.RE_CONSENT

    def test_posting_without_agreeing_rerenders_the_form_with_pending_versions(
        self, client
    ):
        user = UserFactory()
        client.force_login(user)
        version = PolicyVersionFactory(is_active=True)

        response = client.post(reverse("policies:consent"), {})

        assert response.status_code == 200
        assert not PolicyConsent.objects.filter(
            user=user, policy_version=version
        ).exists()

    def test_next_redirects_to_an_allowed_local_path(self, client):
        user = UserFactory()
        client.force_login(user)
        PolicyVersionFactory(is_active=True)

        response = client.post(
            reverse("policies:consent"),
            {"agreed": "on", "next": reverse("challenges:dashboard")},
        )

        assert response.url == reverse("challenges:dashboard")

    def test_next_ignores_an_external_host(self, client):
        user = UserFactory()
        client.force_login(user)
        PolicyVersionFactory(is_active=True)

        response = client.post(
            reverse("policies:consent"),
            {"agreed": "on", "next": "https://evil.example.com/"},
        )

        assert response.url == "/"
