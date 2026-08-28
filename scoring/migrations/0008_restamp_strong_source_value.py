"""Move Strong-sourced PointEarnEvent rows onto "strong_csv" (TASK-332).

PointEarnEvent carries its own copy of the source value (scoring/services.py
copies it off the LiftHistory row it scored, and there is no FK between them --
see TASK-340), so the value rename has to be applied to this table
independently. Same reasoning as
liftosaur/migrations/0009_restamp_strong_source_value.py: lossless, since
Strong has no live-sync path for a "strong" row to have come from.
"""

from django.db import migrations

OLD = "strong"
NEW = "strong_csv"


def strong_to_strong_csv(apps, schema_editor):
    PointEarnEvent = apps.get_model("scoring", "PointEarnEvent")
    PointEarnEvent.objects.filter(source=OLD).update(source=NEW)


def strong_csv_to_strong(apps, schema_editor):
    PointEarnEvent = apps.get_model("scoring", "PointEarnEvent")
    PointEarnEvent.objects.filter(source=NEW).update(source=OLD)


class Migration(migrations.Migration):
    dependencies = [
        ("scoring", "0007_alter_pointearnevent_source"),
    ]

    operations = [
        migrations.RunPython(strong_to_strong_csv, strong_csv_to_strong),
    ]
