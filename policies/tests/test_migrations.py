"""Exercises the 0002 grandfather-consent migration's data function against Postgres.

Runs migrations/0002_grandfather_existing_consent.grandfather_existing_users
directly (the exact function Django's migration runner calls via RunPython)
against the live test database rather than mocking the ORM, so it fails if the
grandfathering logic ever stops matching only currently-active users. It
doesn't re-run the migration through MigrationExecutor: 0001/0002 are already
applied once (for real) by django_db_setup before any test runs, and
re-applying a RunPython migration whose forward side creates unique-slug rows
a second time would collide on that same uniqueness rather than testing
anything new. Deleting the two rows it created and re-invoking the same
function, inside this test's normal transaction (rolled back on teardown),
exercises identical code against the identical tables without that collision.
"""

import importlib

import pytest
from django.apps import apps
from django.urls import reverse

from accounts.tests.factories import UserFactory
from policies.models import Policy, PolicyConsent, PolicyVersion
from policies.services import pending_versions_for

pytestmark = pytest.mark.django_db

grandfather_migration = importlib.import_module(
    "policies.migrations.0002_grandfather_existing_consent"
)
name_glitchtip_migration = importlib.import_module(
    "policies.migrations.0003_name_glitchtip_in_privacy_v1_2"
)


class TestGrandfatherExistingUsers:
    def test_creates_one_active_version_each_for_tos_and_privacy(self):
        Policy.objects.filter(slug__in=["terms-of-service", "privacy-policy"]).delete()

        grandfather_migration.grandfather_existing_users(apps, None)

        tos_version = PolicyVersion.objects.get(
            policy__slug="terms-of-service", is_active=True
        )
        privacy_version = PolicyVersion.objects.get(
            policy__slug="privacy-policy", is_active=True
        )
        assert tos_version.url == reverse("terms")
        assert privacy_version.url == reverse("privacy")

    def test_grandfathers_every_currently_active_user_for_both_policies(self):
        Policy.objects.filter(slug__in=["terms-of-service", "privacy-policy"]).delete()
        active_user = UserFactory(is_active=True)

        grandfather_migration.grandfather_existing_users(apps, None)

        consents = PolicyConsent.objects.filter(user=active_user)
        assert consents.count() == 2
        assert set(consents.values_list("method", flat=True)) == {"ADMIN_OVERRIDE"}

    def test_does_not_grandfather_a_deactivated_user(self):
        Policy.objects.filter(slug__in=["terms-of-service", "privacy-policy"]).delete()
        deactivated_user = UserFactory(is_active=False)

        grandfather_migration.grandfather_existing_users(apps, None)

        assert not PolicyConsent.objects.filter(user=deactivated_user).exists()


class TestAddPrivacyV1_2:
    """Exercises the 0003 migration's data function, which bumps the Privacy
    Policy to v1.2 without grandfathering consent -- unlike 0002, existing
    users are expected to genuinely re-consent."""

    def _seed_v1_1(self):
        Policy.objects.filter(slug__in=["terms-of-service", "privacy-policy"]).delete()
        grandfather_migration.grandfather_existing_users(apps, None)
        return PolicyVersion.objects.get(policy__slug="privacy-policy", version="1.1")

    def test_creates_exactly_one_new_active_v1_2_version(self):
        self._seed_v1_1()

        name_glitchtip_migration.add_privacy_v1_2(apps, None)

        versions = PolicyVersion.objects.filter(
            policy__slug="privacy-policy", version="1.2", is_active=True
        )
        assert versions.count() == 1
        assert versions.get().url == reverse("privacy")

    def test_deactivates_the_prior_active_v1_1_version(self):
        v1_1 = self._seed_v1_1()

        name_glitchtip_migration.add_privacy_v1_2(apps, None)

        v1_1.refresh_from_db()
        assert v1_1.is_active is False

    def test_user_consented_to_v1_1_is_not_consented_to_v1_2(self):
        v1_1 = self._seed_v1_1()
        consented_user = UserFactory(is_active=True)
        PolicyConsent.objects.create(
            user=consented_user,
            policy_version=v1_1,
            method=PolicyConsent.Method.ADMIN_OVERRIDE,
        )

        name_glitchtip_migration.add_privacy_v1_2(apps, None)

        v1_2 = PolicyVersion.objects.get(policy__slug="privacy-policy", version="1.2")
        pending = pending_versions_for(consented_user)
        assert v1_2 in pending
