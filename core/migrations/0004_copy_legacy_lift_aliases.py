"""Copy every row out of the four legacy per-tracker alias tables
(liftosaur.LiftAlias, workout_imports.HevyLiftAlias, workout_imports.StrongLiftAlias,
wger.WgerLiftAlias) into the new unified core.LiftAlias table.

Must run after core.0003 (which creates the table) and after each source
app's latest migration, so their historical models reflect the data that is
actually there. The legacy tables themselves are dropped separately, by each
owning app, in a migration that depends on this one -- so this migration is
the only place the old and new tables coexist, and the only place data can
be lost if something goes wrong.

The reverse migration copies rows back out of core.LiftAlias into the four
legacy tables by source, so this consolidation can be rolled back without
losing data either way.
"""

from django.db import migrations

LEGACY_SOURCES = (
    ("liftosaur", "LiftAlias", "liftosaur"),
    ("workout_imports", "HevyLiftAlias", "hevy"),
    ("workout_imports", "StrongLiftAlias", "strong"),
    ("wger", "WgerLiftAlias", "wger"),
)


def copy_into_unified_table(apps, schema_editor):
    LiftAlias = apps.get_model("core", "LiftAlias")
    rows = []
    for app_label, model_name, source in LEGACY_SOURCES:
        LegacyModel = apps.get_model(app_label, model_name)
        for from_name, to_name in LegacyModel.objects.values_list(
            "from_name", "to_name"
        ):
            rows.append(LiftAlias(source=source, from_name=from_name, to_name=to_name))
    LiftAlias.objects.bulk_create(rows)


def copy_back_into_legacy_tables(apps, schema_editor):
    """Reverse of ``copy_into_unified_table``: restores each legacy table's
    rows, then deletes them from core.LiftAlias -- leaving the schema exactly
    as it was before this migration ever ran forward (data lives in the
    legacy tables only), rather than leaving a duplicate copy sitting in
    core.LiftAlias that a later re-forward would collide with on the
    (source, from_name) uniqueness constraint.
    """
    LiftAlias = apps.get_model("core", "LiftAlias")
    for app_label, model_name, source in LEGACY_SOURCES:
        LegacyModel = apps.get_model(app_label, model_name)
        source_aliases = LiftAlias.objects.filter(source=source)
        rows = [
            LegacyModel(from_name=from_name, to_name=to_name)
            for from_name, to_name in source_aliases.values_list("from_name", "to_name")
        ]
        LegacyModel.objects.bulk_create(rows)
        source_aliases.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_liftalias"),
        ("liftosaur", "0005_alter_lifthistory_source"),
        ("workout_imports", "0002_strongliftalias"),
        ("wger", "0001_wger_integration"),
    ]

    operations = [
        migrations.RunPython(copy_into_unified_table, copy_back_into_legacy_tables),
    ]
