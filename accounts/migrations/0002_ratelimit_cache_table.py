from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    # Creates the DatabaseCache table backing the "ratelimit" cache alias so the
    # request counters are shared across gunicorn workers.
    call_command(
        "createcachetable",
        "ratelimit_cache",
        database=schema_editor.connection.alias,
    )


def drop_cache_table(apps, schema_editor):
    schema_editor.execute("DROP TABLE IF EXISTS ratelimit_cache")


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, drop_cache_table),
    ]
