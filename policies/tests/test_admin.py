import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from policies.models import PolicyNotification
from policies.tests.factories import (
    PolicyConsentFactory,
    PolicyFactory,
    PolicyNotificationFactory,
    PolicyVersionFactory,
)


@pytest.fixture
def staff_client(db):
    user = UserFactory(is_staff=True, is_superuser=True)
    c = Client()
    c.force_login(user)
    return c


@pytest.mark.django_db
class TestPolicyAdmin:
    def test_staff_can_access_policy_changelist(self, staff_client):
        response = staff_client.get(reverse("admin:policies_policy_changelist"))
        assert response.status_code == 200

    def test_active_version_label_shows_the_active_version(self, staff_client):
        policy = PolicyFactory()
        PolicyVersionFactory(policy=policy, version="3.0", is_active=True)

        response = staff_client.get(
            reverse("admin:policies_policy_change", args=[policy.pk])
        )

        assert response.status_code == 200


@pytest.mark.django_db
class TestPolicyVersionAdmin:
    def test_mark_as_active_requires_exactly_one_selected_version(self, staff_client):
        policy = PolicyFactory()
        v1 = PolicyVersionFactory(policy=policy, is_active=True)
        v2 = PolicyVersionFactory(policy=policy, is_active=False)

        staff_client.post(
            reverse("admin:policies_policyversion_changelist"),
            {
                "action": "mark_as_active",
                "_selected_action": [str(v1.pk), str(v2.pk)],
            },
            follow=True,
        )

        v1.refresh_from_db()
        v2.refresh_from_db()
        assert v1.is_active is True
        assert v2.is_active is False

    def test_mark_as_active_activates_the_single_selected_version(self, staff_client):
        policy = PolicyFactory()
        old = PolicyVersionFactory(policy=policy, is_active=True)
        new = PolicyVersionFactory(policy=policy, is_active=False)

        staff_client.post(
            reverse("admin:policies_policyversion_changelist"),
            {"action": "mark_as_active", "_selected_action": [str(new.pk)]},
            follow=True,
        )

        old.refresh_from_db()
        new.refresh_from_db()
        assert old.is_active is False
        assert new.is_active is True

    def test_non_consented_view_lists_users_without_consent(self, staff_client):
        version = PolicyVersionFactory(is_active=True)
        consented_user = UserFactory()
        PolicyConsentFactory(user=consented_user, policy_version=version)
        pending_user = UserFactory(email="pending@example.com")

        response = staff_client.get(
            reverse("admin:policies_policyversion_non_consented", args=[version.pk])
        )

        assert response.status_code == 200
        non_consented_emails = [u.email for u in response.context["non_consented"]]
        assert pending_user.email in non_consented_emails
        assert consented_user.email not in non_consented_emails

    def test_send_notifications_emails_pending_users_with_a_real_consent_link(
        self, staff_client, mailoutbox
    ):
        version = PolicyVersionFactory(is_active=True)
        UserFactory(email="pending@example.com")

        staff_client.post(
            reverse("admin:policies_policyversion_changelist"),
            {"action": "send_notifications", "_selected_action": [str(version.pk)]},
            follow=True,
        )

        # The logged-in staff user is themselves an active, non-consented user
        # and gets notified too -- assert on the pending user's message
        # specifically rather than the total count.
        pending_email = next(m for m in mailoutbox if m.to == ["pending@example.com"])
        assert "/policies/consent/" in pending_email.body


@pytest.mark.django_db
class TestPolicyConsentAdmin:
    def test_change_view_renders_read_only_with_no_save_button(self, staff_client):
        consent = PolicyConsentFactory()

        response = staff_client.get(
            reverse("admin:policies_policyconsent_change", args=[consent.pk])
        )

        assert response.status_code == 200
        assert b'name="_save"' not in response.content

    def test_cannot_add_a_consent_row_via_the_admin(self, staff_client):
        response = staff_client.get(reverse("admin:policies_policyconsent_add"))

        assert response.status_code == 403

    def test_staff_can_still_list_consent_rows(self, staff_client):
        PolicyConsentFactory()

        response = staff_client.get(reverse("admin:policies_policyconsent_changelist"))

        assert response.status_code == 200


@pytest.mark.django_db
class TestPolicyNotificationAdmin:
    def test_change_view_renders_read_only_with_no_save_button(self, staff_client):
        notification = PolicyNotificationFactory()

        response = staff_client.get(
            reverse("admin:policies_policynotification_change", args=[notification.pk])
        )

        assert response.status_code == 200
        assert b'name="_save"' not in response.content

    def test_cannot_add_a_notification_row_via_the_admin(self, staff_client):
        response = staff_client.get(reverse("admin:policies_policynotification_add"))

        assert response.status_code == 403

    def test_changelist_search_resolves_every_search_field(self, staff_client):
        # search_fields spans two relations (user__email,
        # policy_version__policy__name); a typo in either raises FieldError
        # only once an operator searches, not on a plain changelist GET.
        PolicyNotificationFactory(user=UserFactory(email="notified@example.com"))

        response = staff_client.get(
            reverse("admin:policies_policynotification_changelist"),
            {"q": "notified@example.com"},
        )

        assert response.status_code == 200
        assert b"notified@example.com" in response.content

    def test_cannot_delete_a_notification_row_via_the_admin(self, staff_client):
        # This log is the evidence that a policy-update notice went out, so an
        # operator must not be able to erase a row from it.
        notification = PolicyNotificationFactory()

        response = staff_client.post(
            reverse("admin:policies_policynotification_delete", args=[notification.pk]),
            {"post": "yes"},
        )

        assert response.status_code == 403
        assert PolicyNotification.objects.filter(pk=notification.pk).exists()
