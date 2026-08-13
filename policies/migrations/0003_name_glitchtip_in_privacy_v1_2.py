"""Bump the Privacy Policy to v1.2, naming GlitchTip explicitly.

v1.1 described the error-tracking and log-aggregation destination for server
logs generically. v1.2 (see templates/legal/privacy.html) names it explicitly
as GlitchTip, self-hosted on infrastructure we control. Unlike 0002, this
migration does not grandfather existing consent -- this is a real content
change, not an internal rollout, so existing users are expected to
re-consent to v1.2 on their next request.

Note: PolicyVersion.save() normally auto-deactivates the prior active version
for the same policy, but that override does not run when going through the
historical model API here, so the previously active version is deactivated
explicitly before the new one is created.
"""

from datetime import date

from django.db import migrations
from django.urls import reverse

EFFECTIVE_DATE = date(2026, 8, 19)
VERSION = "1.2"


def add_privacy_v1_2(apps, schema_editor):
    Policy = apps.get_model("policies", "Policy")
    PolicyVersion = apps.get_model("policies", "PolicyVersion")

    privacy_policy = Policy.objects.get(slug="privacy-policy")

    PolicyVersion.objects.filter(policy__slug="privacy-policy", is_active=True).update(
        is_active=False
    )

    PolicyVersion.objects.create(
        policy=privacy_policy,
        version=VERSION,
        url=reverse("privacy"),
        effective_date=EFFECTIVE_DATE,
        is_active=True,
        changelog=(
            "Named GlitchTip (self-hosted) explicitly as the error-tracking/"
            "log-aggregation destination described generically in v1.1."
        ),
    )


def noop_reverse(apps, schema_editor):
    """Deliberately does not delete rows -- no hard deletes (see AGENTS.md)."""


class Migration(migrations.Migration):
    dependencies = [
        ("policies", "0002_grandfather_existing_consent"),
    ]

    operations = [
        migrations.RunPython(add_privacy_v1_2, noop_reverse),
    ]
