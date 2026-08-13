import pytest
from django.core import mail

from accounts.tests.factories import UserFactory
from policies.models import PolicyNotification
from policies.notifications import notify_users_for_version
from policies.tests.factories import (
    PolicyConsentFactory,
    PolicyVersionFactory,
)

pytestmark = pytest.mark.django_db


class TestNotifyUsersForVersion:
    def test_emails_an_active_user_who_has_not_consented(self, rf):
        version = PolicyVersionFactory()
        user = UserFactory(email="lifter@example.com")

        count = notify_users_for_version(version, rf.get("/"))

        assert count == 1
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["lifter@example.com"]
        assert PolicyNotification.objects.filter(
            user=user, policy_version=version
        ).exists()

    def test_skips_a_user_who_already_consented(self, rf):
        version = PolicyVersionFactory()
        consent = PolicyConsentFactory(policy_version=version)

        count = notify_users_for_version(version, rf.get("/"))

        assert count == 0
        assert not PolicyNotification.objects.filter(
            user=consent.user, policy_version=version
        ).exists()

    def test_skips_a_user_already_notified(self, rf):
        version = PolicyVersionFactory()
        user = UserFactory(email="lifter@example.com")
        notify_users_for_version(version, rf.get("/"))
        mail.outbox.clear()

        count = notify_users_for_version(version, rf.get("/"))

        assert count == 0
        assert len(mail.outbox) == 0
        assert (
            PolicyNotification.objects.filter(user=user, policy_version=version).count()
            == 1
        )

    def test_skips_a_deactivated_user(self, rf):
        version = PolicyVersionFactory()
        UserFactory(email="gone@example.com", is_active=False)

        count = notify_users_for_version(version, rf.get("/"))

        assert count == 0
        assert len(mail.outbox) == 0

    def test_dry_run_counts_without_sending_or_recording(self, rf):
        version = PolicyVersionFactory()
        UserFactory(email="lifter@example.com")

        count = notify_users_for_version(version, rf.get("/"), dry_run=True)

        assert count == 1
        assert len(mail.outbox) == 0
        assert not PolicyNotification.objects.exists()

    def test_builds_consent_link_from_the_request(self, rf, settings):
        settings.ALLOWED_HOSTS = ["verylift.example.com"]
        version = PolicyVersionFactory()
        UserFactory(email="lifter@example.com")

        notify_users_for_version(
            version, rf.get("/", SERVER_NAME="verylift.example.com", secure=True)
        )

        assert "https://verylift.example.com/policies/consent/" in mail.outbox[0].body
