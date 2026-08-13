import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Policy",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("slug", models.SlugField(unique=True)),
                (
                    "policy_type",
                    models.CharField(
                        choices=[
                            ("TOS", "Terms of Service"),
                            ("PRIVACY", "Privacy Policy"),
                            ("COOKIE", "Cookie Policy"),
                            ("EULA", "EULA"),
                            ("OTHER", "Other"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "requires_consent",
                    models.BooleanField(
                        default=True,
                        help_text="Whether this policy requires tracked user acceptance.",
                    ),
                ),
                (
                    "gates_access",
                    models.BooleanField(
                        default=True,
                        help_text="Whether non-acceptance blocks web app access.",
                    ),
                ),
                ("description", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name_plural": "policies",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="PolicyVersion",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("version", models.CharField(max_length=50)),
                (
                    "url",
                    models.URLField(help_text="Link to the actual document content."),
                ),
                ("effective_date", models.DateField()),
                (
                    "is_active",
                    models.BooleanField(
                        default=False,
                        help_text="Only one active version per policy at a time.",
                    ),
                ),
                (
                    "changelog",
                    models.TextField(
                        blank=True,
                        help_text="Summary of what changed from the prior version.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "policy",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="versions",
                        to="policies.policy",
                    ),
                ),
            ],
            options={
                "ordering": ["-effective_date"],
            },
        ),
        migrations.CreateModel(
            name="PolicyNotification",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("notified_at", models.DateTimeField(auto_now_add=True)),
                (
                    "method",
                    models.CharField(
                        choices=[("EMAIL", "Email")], default="EMAIL", max_length=20
                    ),
                ),
                (
                    "policy_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notifications",
                        to="policies.policyversion",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="policy_notifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "policy_version"),
                        name="unique_user_policy_notification",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="PolicyConsent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("consented_at", models.DateTimeField(auto_now_add=True)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.TextField(blank=True)),
                (
                    "method",
                    models.CharField(
                        choices=[
                            ("SIGNUP", "Signup"),
                            ("RE_CONSENT", "Re-consent"),
                            ("ADMIN_OVERRIDE", "Admin Override"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "policy_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="consents",
                        to="policies.policyversion",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="policy_consents",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-consented_at"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "policy_version"),
                        name="unique_user_policy_version",
                    )
                ],
            },
        ),
    ]
