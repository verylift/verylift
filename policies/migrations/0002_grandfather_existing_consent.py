"""Seed the ToS/Privacy policy rows and grandfather existing users' consent.

Rolling out consent tracking must not force every already-registered user to
re-consent on their next request -- the gating middleware only bites on the
*next* real version bump after this ships. To make that true, this migration
creates one active PolicyVersion each for Terms of Service and Privacy Policy
(pointing at verylift's existing /terms/ and /privacy/ routes, matching the
version/date already shown on those pages -- see templates/legal/terms.html
and templates/legal/privacy.html) and records a PolicyConsent row with
method=ADMIN_OVERRIDE for every currently-active user against both versions.
"""

from datetime import date

from django.conf import settings
from django.db import migrations
from django.urls import reverse

EFFECTIVE_DATE = date(2026, 8, 12)
VERSION = "1.1"


def grandfather_existing_users(apps, schema_editor):
    Policy = apps.get_model("policies", "Policy")
    PolicyVersion = apps.get_model("policies", "PolicyVersion")
    PolicyConsent = apps.get_model("policies", "PolicyConsent")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    tos_policy = Policy.objects.create(
        name="Terms of Service",
        slug="terms-of-service",
        policy_type="TOS",
        requires_consent=True,
        gates_access=True,
        description="verylift's Terms of Service.",
    )
    privacy_policy = Policy.objects.create(
        name="Privacy Policy",
        slug="privacy-policy",
        policy_type="PRIVACY",
        requires_consent=True,
        gates_access=True,
        description="verylift's Privacy Policy.",
    )

    tos_version = PolicyVersion.objects.create(
        policy=tos_policy,
        version=VERSION,
        url=reverse("terms"),
        effective_date=EFFECTIVE_DATE,
        is_active=True,
        changelog="Initial version tracked by the policies app.",
    )
    privacy_version = PolicyVersion.objects.create(
        policy=privacy_policy,
        version=VERSION,
        url=reverse("privacy"),
        effective_date=EFFECTIVE_DATE,
        is_active=True,
        changelog="Initial version tracked by the policies app.",
    )

    active_user_ids = list(
        User.objects.filter(is_active=True).values_list("id", flat=True)
    )
    PolicyConsent.objects.bulk_create(
        [
            PolicyConsent(
                user_id=user_id,
                policy_version=version,
                method="ADMIN_OVERRIDE",
            )
            for version in (tos_version, privacy_version)
            for user_id in active_user_ids
        ],
        ignore_conflicts=True,
    )


def noop_reverse(apps, schema_editor):
    """Deliberately does not delete rows -- no hard deletes (see AGENTS.md)."""


class Migration(migrations.Migration):
    dependencies = [
        ("policies", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(grandfather_existing_users, noop_reverse),
    ]
