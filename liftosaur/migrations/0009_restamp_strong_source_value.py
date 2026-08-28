"""Move existing Strong-sourced rows onto the new "strong_csv" value (TASK-332).

The LiftSource convention change made a bare ``<tracker>`` value mean the live
API path, so ``STRONG_CSV``'s stored value became "strong_csv" rather than
staying "strong" -- otherwise Strong's CSV rows would read as API rows under
the new rule, and a future Strong live-sync integration would find its
natural value already occupied (exactly the collision this convention exists
to remove).

Unlike the Liftosaur rows, which stay deliberately collided because they are
an unseparable mix of API-sync and CSV-import writes, this rename is lossless
and unambiguous: Strong has no API, so every "strong" row is a CSV import by
construction. Safe to rewrite in both directions.

Production held zero Strong rows when this was written (2026-08-28), so this
is a no-op there; it exists for development and pre-production databases that
do have them.
"""

from django.db import migrations

OLD = "strong"
NEW = "strong_csv"


def strong_to_strong_csv(apps, schema_editor):
    LiftHistory = apps.get_model("liftosaur", "LiftHistory")
    LiftHistory.objects.filter(source=OLD).update(source=NEW)


def strong_csv_to_strong(apps, schema_editor):
    LiftHistory = apps.get_model("liftosaur", "LiftHistory")
    LiftHistory.objects.filter(source=NEW).update(source=OLD)


class Migration(migrations.Migration):
    dependencies = [
        ("liftosaur", "0008_alter_lifthistory_source"),
    ]

    operations = [
        migrations.RunPython(strong_to_strong_csv, strong_csv_to_strong),
    ]
