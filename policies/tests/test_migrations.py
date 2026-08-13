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

pytestmark = pytest.mark.django_db

grandfather_migration = importlib.import_module(
    "policies.migrations.0002_grandfather_existing_consent"
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
