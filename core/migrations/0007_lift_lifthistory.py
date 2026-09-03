"""Adopt Lift and LiftHistory into core, in Django state only (TASK-347).

These models were never Liftosaur-specific -- Lift is the canonical lift
register and LiftHistory is the pool every tracker sync and CSV importer
writes into -- but they grew inside the liftosaur app because it was the first
integration built. 34 modules outside that app imported them from it,
including wger and hevy_api reaching into a competitor tracker's app for their
core domain types.

Nothing happens to the database here. ``db_table`` is pinned to the existing
"liftosaur_lift" / "liftosaur_lifthistory" names and the CreateModel pair is
wrapped in SeparateDatabaseAndState with no database_operations, so Django's
model state moves app while the tables, their rows, their indexes and their
constraints stay exactly where they are. liftosaur.0011 performs the matching
state-only DeleteModel and depends on this migration, so the two halves can
never apply out of order and leave the models defined twice.

Renaming the tables to match the new app label would be a real data migration
with real downtime risk, and is deliberately NOT bundled here.
"""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_copy_fitnessvolt_lift_aliases"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.CreateModel(
                    name="Lift",
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
                        ("name", models.CharField(max_length=100, unique=True)),
                        (
                            "is_bodyweight_added",
                            models.BooleanField(
                                default=False,
                                help_text="The strength standard threshold for this lift already includes the lifter's bodyweight; the recorded weight is the added weight on top of bodyweight (e.g. Pull-up, Chin-up, Dip).",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "liftosaur_lift",
                        "ordering": ["name"],
                    },
                ),
                migrations.CreateModel(
                    name="LiftHistory",
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
                        ("lift", models.CharField(max_length=100)),
                        ("performed_at", models.DateField()),
                        (
                            "weight_kg",
                            models.DecimalField(decimal_places=2, max_digits=7),
                        ),
                        ("reps", models.PositiveIntegerField()),
                        (
                            "equipment",
                            models.CharField(
                                blank=True,
                                default="",
                                help_text="Equipment/variant suffix from the Liftosaur history line (e.g. 'Leverage Machine'); empty when the set named no equipment. Distinguishes assisted-machine sets, whose recorded weight is already the net total load, from free bodyweight or added-weight sets of the same lift.",
                                max_length=100,
                            ),
                        ),
                        ("synced_at", models.DateTimeField(blank=True, null=True)),
                        (
                            "source",
                            models.CharField(
                                choices=[
                                    ("liftosaur", "Liftosaur"),
                                    ("liftosaur_csv", "Liftosaur (CSV import)"),
                                    ("manual", "Manual"),
                                    ("hevy", "Hevy"),
                                    ("hevy_csv", "Hevy (CSV import)"),
                                    ("wger", "Wger"),
                                    ("strong_csv", "Strong (CSV import)"),
                                ],
                                default="liftosaur",
                                help_text="Where this set came from: a Liftosaur sync pull, or a lifter self-reporting a completed set with no tracker connected.",
                                max_length=20,
                            ),
                        ),
                        (
                            "user",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.PROTECT,
                                related_name="lift_history",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                    ],
                    options={
                        "db_table": "liftosaur_lifthistory",
                        "ordering": ["-performed_at"],
                        "unique_together": {
                            ("user", "lift", "performed_at", "reps", "weight_kg")
                        },
                    },
                ),
            ],
        ),
    ]
