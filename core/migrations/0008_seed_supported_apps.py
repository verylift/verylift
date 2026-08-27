"""Seed the four workout-tracking apps verified against the codebase's actual
integrations for the "supported apps" page (TASK-254): Liftosaur and Hevy
each have a live-sync client (liftosaur/client.py, hevy_api/client.py) and a
CSV importer (workout_imports/importers/{liftosaur,hevy}.py); Wger has only
a live-sync client; Strong has only a CSV importer. No affiliate URLs are
live yet -- Liftosaur is marked is_affiliate=True because the existing
Liftosaur coupon-code relationship (see
templates/components/_liftosaur_coupon_cta.html) already makes it one, but
its url here is the same plain, untracked link used there, not a tracked
affiliate URL.
"""

from django.db import migrations

APPS = [
    {
        "name": "Liftosaur",
        "url": "https://www.liftosaur.com",
        "is_affiliate": True,
        "sort_order": 0,
        "description": "Automatically syncs every lift you log, or upload a "
        "CSV export if you'd rather not connect the API. We, ourselves, "
        "love using Liftosaur.",
        "modes": ["live_sync", "csv_upload"],
    },
    {
        "name": "Hevy",
        "url": "https://www.hevyapp.com",
        "is_affiliate": False,
        "sort_order": 1,
        "description": "Connect your Hevy account for automatic sync, or "
        "upload a CSV export instead.",
        "modes": ["live_sync", "csv_upload"],
    },
    {
        "name": "Wger",
        "url": "https://wger.de",
        "is_affiliate": False,
        "sort_order": 2,
        "description": "Self-hosted and open source. Connect your own Wger "
        "instance with an API token.",
        "modes": ["live_sync"],
    },
    {
        "name": "Strong",
        "url": "https://www.strongapp.io",
        "is_affiliate": False,
        "sort_order": 3,
        "description": "Export your history from Strong and upload the "
        "file — we'll do the rest.",
        "modes": ["csv_upload"],
    },
]


def seed_apps(apps, schema_editor):
    SupportedApp = apps.get_model("core", "SupportedApp")
    SupportedAppMode = apps.get_model("core", "SupportedAppMode")
    for entry in APPS:
        app = SupportedApp.objects.create(
            name=entry["name"],
            url=entry["url"],
            is_affiliate=entry["is_affiliate"],
            sort_order=entry["sort_order"],
            description=entry["description"],
        )
        SupportedAppMode.objects.bulk_create(
            SupportedAppMode(supported_app=app, mode=mode) for mode in entry["modes"]
        )


def remove_seeded_apps(apps, schema_editor):
    SupportedApp = apps.get_model("core", "SupportedApp")
    SupportedApp.objects.filter(name__in=[e["name"] for e in APPS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0007_supportedapp_supportedappmode"),
    ]

    operations = [
        migrations.RunPython(seed_apps, remove_seeded_apps),
    ]
